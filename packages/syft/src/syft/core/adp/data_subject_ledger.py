# future
from __future__ import annotations

# stdlib
from functools import partial
import os
from pathlib import Path
import time
from typing import Any
from typing import Callable
from typing import Final
from typing import Optional
from typing import TYPE_CHECKING
from typing import Tuple

if TYPE_CHECKING:
    # stdlib
    from dataclasses import dataclass
else:
    from flax.struct import dataclass

# third party
import jax
from jax import numpy as jnp
from nacl.signing import VerifyKey
import numpy as np
from scipy.optimize import minimize_scalar

# relative
from ...core.node.common.node_manager.user_manager import RefreshBudgetException
from ...lib.numpy.array import arrow_deserialize as numpy_deserialize
from ...lib.numpy.array import arrow_serialize as numpy_serialize
from ..common.serde.capnp import CapnpModule
from ..common.serde.capnp import chunk_bytes
from ..common.serde.capnp import combine_bytes
from ..common.serde.capnp import get_capnp_schema
from ..common.serde.capnp import serde_magic_header
from ..common.serde.serializable import serializable
from .abstract_ledger_store import AbstractDataSubjectLedger
from .abstract_ledger_store import AbstractLedgerStore


def get_cache_path(cache_filename: str) -> str:
    here = os.path.dirname(__file__)
    root_dir = Path(here) / ".." / ".." / "cache"
    return os.path.abspath(root_dir / cache_filename)


def load_cache(filename: str) -> np.ndarray:
    CACHE_PATH = get_cache_path(filename)
    if not os.path.exists(CACHE_PATH):
        raise Exception(f"Cannot load {CACHE_PATH}")
    cache_array = np.load(CACHE_PATH)
    print(f"Loaded constant2epsilon cache of size: {cache_array.shape}")
    return cache_array


@dataclass
class RDPParams:
    sigmas: jnp.array
    l2_norms: jnp.array
    l2_norm_bounds: jnp.array
    Ls: jnp.array
    coeffs: jnp.array


@partial(jax.jit, static_argnums=3, donate_argnums=(1, 2))
def first_try_branch(
    constant: jax.numpy.DeviceArray,
    rdp_constants: np.ndarray,
    entity_ids_query: np.ndarray,
    max_entity: int,
) -> jax.numpy.DeviceArray:
    summed_constant = constant + rdp_constants.take(entity_ids_query)
    if max_entity < len(rdp_constants):
        return rdp_constants.at[entity_ids_query].set(summed_constant)
    else:
        pad_length = max_entity - len(rdp_constants) + 1
        rdp_constants = jnp.concatenate([rdp_constants, jnp.zeros(shape=pad_length)])
        summed_constant = constant + rdp_constants.take(entity_ids_query)
        return rdp_constants.at[entity_ids_query].set(summed_constant)


@partial(jax.jit, static_argnums=1)
def compute_rdp_constant(rdp_params: RDPParams, private: bool) -> jax.numpy.DeviceArray:
    squared_Ls = rdp_params.Ls**2
    squared_sigma = rdp_params.sigmas**2

    if private:
        # this is calculated on the private true values
        squared_l2 = rdp_params.l2_norms**2
    else:
        # bounds is computed on the metadata
        squared_l2 = rdp_params.l2_norm_bounds**2

    return (squared_Ls * squared_l2 / (2 * squared_sigma)) * rdp_params.coeffs


@jax.jit
def get_budgets_and_mask(
    epsilon_spend: jnp.array, user_budget: jnp.float64
) -> Tuple[float, float, jax.numpy.DeviceArray]:
    # Function to vectorize the result of the budget computation.
    mask = jnp.ones_like(epsilon_spend) * user_budget < epsilon_spend
    # get the highest value which was under budget and represented by False in the mask
    highest_possible_spend = jnp.max(epsilon_spend * (1 - mask))
    return (highest_possible_spend, user_budget, mask)


@serializable(capnp_bytes=True)
class DataSubjectLedger(AbstractDataSubjectLedger):
    """for a particular data subject, this is the list
    of all mechanisms releasing informationo about this
    particular subject, stored in a vectorized form"""

    CONSTANT2EPSILSON_CACHE_FILENAME = "constant2epsilon_300k.npy"
    _cache_constant2epsilon = load_cache(filename=CONSTANT2EPSILSON_CACHE_FILENAME)

    def __init__(
        self,
        constants: Optional[np.ndarray] = None,
        update_number: int = 0,
        timestamp_of_last_update: Optional[float] = None,
    ) -> None:
        self._rdp_constants = (
            constants if constants is not None else np.array([], dtype=np.float64)
        )
        self._update_number = update_number
        self._timestamp_of_last_update = (
            timestamp_of_last_update
            if timestamp_of_last_update is not None
            else time.time()
        )
        self._pending_save = False

    @property
    def delta(self) -> float:
        FIXED_DELTA: Final = 1e-6
        return FIXED_DELTA  # WARNING: CHANGING DELTA INVALIDATES THE CACHE

    def bind_to_store_with_key(
        self, store: AbstractLedgerStore, user_key: VerifyKey
    ) -> None:
        self.store = store
        self.user_key = user_key

    @staticmethod
    def get_or_create(
        store: AbstractLedgerStore, user_key: VerifyKey
    ) -> Optional[AbstractDataSubjectLedger]:
        ledger: Optional[AbstractDataSubjectLedger] = None
        try:
            # todo change user_key or uid?
            ledger = store.get(key=user_key)
            ledger.bind_to_store_with_key(store=store, user_key=user_key)
        except KeyError:
            print("Creating new Ledger")
            ledger = DataSubjectLedger()
            ledger.bind_to_store_with_key(store=store, user_key=user_key)
        except Exception as e:
            print(f"Failed to read ledger from ledger store. {e}")

        return ledger

    def get_entity_overbudget_mask_for_epsilon_and_append(
        self,
        unique_entity_ids_query: np.ndarray,
        rdp_params: RDPParams,
        node: Any,
        private: bool = True,
    ) -> np.ndarray:
        # coerce to np.int64
        entity_ids_query: np.ndarray = unique_entity_ids_query.astype(np.int64)

        print("RDP constants calculation Batch")

        t1 = time.time()
        # calculate constants
        rdp_constants = self._get_batch_rdp_constants(
            entity_ids_query=entity_ids_query, rdp_params=rdp_params, private=private
        )
        t2 = time.time()
        print("Get Batch RDP Constants Time", t2 - t1)

        print("start Overbudgetted entities calculation")
        # here we iteratively attempt to calculate the overbudget mask and save
        # changes to the database
        t1 = time.time()
        mask = self._get_overbudgeted_entities(
            node=node,
            rdp_constants=rdp_constants,
        )
        t2 = time.time()
        print("Overbudgeted Entitities time ", t2 - t1)

        # at this point we are confident that the database budget field has been updated
        # so now we should flush the _rdp_constants that we have calculated to storage
        if self._write_ledger(node=node):
            return mask

    def _write_ledger(self, node: Any) -> bool:

        self._update_number += 1
        try:
            self._pending_save = False
            self.store.set(key=self.user_key, value=self)
            return True
        except Exception as e:
            self._pending_save = True
            print(f"Failed to write ledger to ledger store. {e}")
            raise e

    def _increase_max_cache(self, new_size: int) -> None:
        new_entries = []
        current_size = len(self._cache_constant2epsilon)
        new_alphas = []
        for i in range(new_size - current_size):
            alph, eps = self._get_optimal_alpha_for_constant(
                constant=i + 1 + current_size
            )
            new_entries.append(eps)
            new_alphas.append(alph)
        # print("The values of epsilon created were: ")
        # print(new_entries)

        # print("The values of alpha  used  for these epsilon were:")
        # print(new_alphas)
        self._cache_constant2epsilon = np.concatenate(
            [self._cache_constant2epsilon, np.array(new_entries)]
        )

    def _get_fake_rdp_func(self, constant: int) -> Callable:
        def func(alpha: float) -> float:
            return alpha * constant

        return func

    def _get_alpha_search_function(self, rdp_compose_func: Callable) -> Callable:
        # if len(self.deltas) > 0:
        # delta = np.max(self.deltas)
        # else:
        log_delta = np.log(self.delta)

        def fun(alpha: float) -> float:  # the input is the RDP's \alpha
            if alpha <= 1:
                return np.inf
            else:
                alpha_minus_1 = alpha - 1
                return np.maximum(
                    rdp_compose_func(alpha)
                    + np.log(alpha_minus_1 / alpha)
                    - (log_delta + np.log(alpha)) / alpha_minus_1,
                    0,
                )

        return fun

    def _get_optimal_alpha_for_constant(
        self, constant: int = 3
    ) -> Tuple[np.ndarray, Callable]:
        f = self._get_fake_rdp_func(constant=constant)
        f2 = self._get_alpha_search_function(rdp_compose_func=f)
        results = minimize_scalar(
            f2, method="Brent", bracket=(1, 2), bounds=[1, np.inf]
        )

        return results.x, results.fun

    def _get_batch_rdp_constants(
        self, entity_ids_query: jnp.ndarray, rdp_params: RDPParams, private: bool = True
    ) -> jnp.ndarray:
        constant = compute_rdp_constant(rdp_params, private)
        print("rdp_constants", type(self._rdp_constants), self._rdp_constants.shape)
        if self._rdp_constants.size == 0:
            self._rdp_constants = np.zeros_like(np.asarray(constant, constant.dtype))
        self._rdp_constants = first_try_branch(
            constant,
            self._rdp_constants,
            entity_ids_query,
            int(jnp.max(entity_ids_query)),
        )
        return constant

    # def _get_batch_rdp_constants(
    #     self, entity_ids_query: np.ndarray, rdp_params: RDPParams, private: bool = True
    # ) -> np.ndarray:
    #     if self._pending_save:
    #         raise Exception(
    #             "We need to save the DataSubjectLedger before querying again"
    #         )
    #     self._pending_save = True

    #     t1 = time.time()
    #     constant = compute_rdp_constant(rdp_params, private)

    #     t2 = time.time()
    #     print("First Block ", t2 - t1)
    #     # update our serialized format with the calculated constants
    #     # extend to and += _rdp_constants
    #     # TODO: test take and put because these are hairy
    #     # TODO: figure out what is faster between max() and take / put
    #     t1 = time.time()

    #     try:
    #         # add the calculated constants back to the cached _rdp_constants
    #         summed_constant = constant + self._rdp_constants.take(entity_ids_query)
    #         self._rdp_constants.put(entity_ids_query, summed_constant)
    #     except IndexError:
    #         new_length = np.max(
    #             entity_ids_query
    #         )  # the highest int is the highest entity id

    #         old_length = len(self._rdp_constants)
    #         if new_length <= old_length:
    #             raise Exception(
    #                 "We have an IndexError but _rdp_constants is big enough."
    #                 + f"{new_length} {old_length}"
    #             )
    #         # figure out how many more spots we need in the array
    #         pad_length = new_length - old_length
    #         # if entity_ids_query is not 0 indexed, e.g. starts at 1, we will have
    #         # 1 extra length required hence the pad_length + 1
    #         # if not, we have 1 extra slot so who cares
    #         self._rdp_constants = np.pad(
    #             self._rdp_constants, pad_width=(0, pad_length + 1), constant_values=0
    #         )

    #         # one more time... lets celebrate
    #         try:
    #             summed_constant = constant + self._rdp_constants.take(entity_ids_query)
    #             self._rdp_constants.put(entity_ids_query, summed_constant)
    #         except IndexError as e:
    #             print("Something really bad happened with np.pad.")
    #             raise e

    #     t2 = time.time()
    #     print("Second Batch", t2 - t1)

    #     return constant

    def _get_epsilon_spend(self, rdp_constants: np.ndarray) -> np.ndarray:
        rdp_constants_lookup = (rdp_constants - 1).astype(np.int64)
        try:
            # needed as np.int64 to use take
            eps_spend = jax.jit(jnp.take)(
                self._cache_constant2epsilon, rdp_constants_lookup
            )
        except IndexError:
            print(f"Cache missed the value at {max(rdp_constants_lookup)}")
            self._increase_max_cache(int(max(rdp_constants_lookup) * 1.1))
            eps_spend = jax.jit(jnp.take)(
                self._cache_constant2epsilon, rdp_constants_lookup
            )
        return eps_spend

    def _calculate_mask_for_current_budget(
        self, node: Any, epsilon_spend: np.ndarray
    ) -> Tuple[float, float, np.ndarray]:
        user_budget = node.users.get_budget_for_user(verify_key=self.user_key)
        # create a mask of True and False where true is over current user_budget
        return get_budgets_and_mask(epsilon_spend, user_budget)

    def _get_overbudgeted_entities(
        self,
        node: Any,
        rdp_constants: np.ndarray,
    ) -> Tuple[np.ndarray]:
        """TODO:
        In our current implementation, user_budget is obtained by querying the
        Adversarial Accountant's entity2ledger with the Data Scientist's User Key.
        When we replace the entity2ledger with something else, we could perhaps directly
        add it into this method
        """
        print("rdp_constants")
        # print(min(rdp_constants))
        # Get the privacy budget spent by all the entities

        t1 = time.time()
        epsilon_spend = self._get_epsilon_spend(rdp_constants=rdp_constants)
        t2 = time.time()
        print("GET Epsilon Spend time", t2 - t1)

        t1 = time.time()
        # try first time
        (
            highest_possible_spend,
            user_budget,
            mask,
        ) = self._calculate_mask_for_current_budget(
            node=node, epsilon_spend=epsilon_spend
        )
        t2 = time.time()
        print("Mask calculation time , ", t2 - t1)
        print("Type of Mask", type(mask))
        print("highest_possible_spend", type(highest_possible_spend))
        print("user budget", type(user_budget))
        mask = np.array(mask, copy=False)
        highest_possible_spend = float(highest_possible_spend)
        user_budget = float(user_budget)
        print("New Type mask", mask)
        print("new high", highest_possible_spend)
        print(" new user budget", user_budget)

        if highest_possible_spend > 0:
            # go spend it in the db
            attempts = 0
            while attempts < 5:
                print(
                    f"Attemping to spend epsilon: {highest_possible_spend}. Try: {attempts}"
                )
                attempts += 1
                try:
                    t1 = time.time()
                    user_budget = self.spend_epsilon(
                        node=node,
                        epsilon_spend=highest_possible_spend,
                        old_user_budget=user_budget,
                    )
                    t2 = time.time()
                    print("Spend Epsilon time", t2 - t1)
                    break
                except RefreshBudgetException as e:
                    # this is the only exception we allow to retry
                    print("got refresh budget error", e)
                    (
                        highest_possible_spend,
                        user_budget,
                        mask,
                    ) = self._calculate_mask_for_current_budget(
                        node=node, epsilon_spend=epsilon_spend
                    )
                except Exception as e:
                    print(f"Problem spending epsilon. {e}")
                    raise e

        if user_budget is None:
            raise Exception("Failed to spend_epsilon")

        return mask

    def spend_epsilon(
        self,
        node: Any,
        epsilon_spend: float,
        old_user_budget: float,
    ) -> float:
        # get the budget
        print("got user budget", old_user_budget, "epsilon_spent", epsilon_spend)
        node.users.deduct_epsilon_for_user(
            verify_key=self.user_key,
            old_budget=old_user_budget,
            epsilon_spend=epsilon_spend,
        )
        # return the budget we used
        return old_user_budget

    def _object2bytes(self) -> bytes:
        schema = get_capnp_schema(schema_file="data_subject_ledger.capnp")

        dsl_struct: CapnpModule = schema.DataSubjectLedger  # type: ignore
        dsl_msg = dsl_struct.new_message()
        metadata_schema = dsl_struct.TensorMetadata
        constants_metadata = metadata_schema.new_message()

        # this is how we dispatch correct deserialization of bytes
        dsl_msg.magicHeader = serde_magic_header(type(self))

        self._rdp_constants = np.array(self._rdp_constants, copy=False)
        constants, constants_size = numpy_serialize(self._rdp_constants, get_bytes=True)
        chunk_bytes(constants, "constants", dsl_msg)
        constants_metadata.dtype = str(self._rdp_constants.dtype)
        constants_metadata.decompressedSize = constants_size
        dsl_msg.constantsMetadata = constants_metadata

        dsl_msg.updateNumber = self._update_number
        print(
            self._timestamp_of_last_update,
            "self._timestamp_of_last_update",
            type(self._timestamp_of_last_update),
        )
        dsl_msg.timestamp = self._timestamp_of_last_update

        return dsl_msg.to_bytes_packed()

    @staticmethod
    def _bytes2object(buf: bytes) -> DataSubjectLedger:
        schema = get_capnp_schema(schema_file="data_subject_ledger.capnp")
        dsl_struct: CapnpModule = schema.DataSubjectLedger  # type: ignore
        # https://stackoverflow.com/questions/48458839/capnproto-maximum-filesize
        MAX_TRAVERSAL_LIMIT = 2**64 - 1
        # to pack or not to pack?
        # ndept_msg = ndept_struct.from_bytes(buf, traversal_limit_in_words=2 ** 64 - 1)
        dsl_msg = dsl_struct.from_bytes_packed(
            buf, traversal_limit_in_words=MAX_TRAVERSAL_LIMIT
        )

        constants_metadata = dsl_msg.constantsMetadata

        constants = numpy_deserialize(
            combine_bytes(dsl_msg.constants),
            constants_metadata.decompressedSize,
            constants_metadata.dtype,
        )
        update_number = dsl_msg.updateNumber
        timestamp_of_last_update = dsl_msg.timestamp

        return DataSubjectLedger(
            constants=constants,
            update_number=update_number,
            timestamp_of_last_update=timestamp_of_last_update,
        )
