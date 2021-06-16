from hub.htypes import DEFAULT_HTYPE
from hub.core.meta.tensor_meta import TensorMeta
from hub.core.meta.index_meta import IndexMeta
from hub.core.index import Index
from typing import List, Tuple, Union
import numpy as np

from hub.core.chunk_engine.read import sample_from_index_entry
from hub.core.chunk_engine.write import write_bytes
from hub.util.keys import get_index_meta_key, get_tensor_meta_key
from hub.util.array import normalize_and_batchify_array_shape
from hub.core.typing import StorageProvider
from hub.util.exceptions import (
    DynamicTensorNumpyError,
    TensorAlreadyExistsError,
    TensorInvalidSampleShapeError,
    TensorMetaMismatchError,
    TensorDoesNotExistError,
)
from .flatten import row_wise_to_bytes


def tensor_exists(key: str, storage: StorageProvider) -> bool:
    """A tensor exists if at the specified `key` and `storage` there is both a tensor meta file and index map."""

    meta_key = get_tensor_meta_key(key)
    index_meta_key = get_index_meta_key(key)
    return meta_key in storage and index_meta_key in storage


def create_tensor(
    key: str,
    storage: StorageProvider,
    htype: str = DEFAULT_HTYPE,
    **kwargs,
):
    """If a tensor does not exist, create a new one with the provided meta.

    Args:
        key (str): Key for where the chunks, index_meta, and tensor_meta will be located in `storage` relative to it's root.
        storage (StorageProvider): StorageProvider that all tensor data is written to.
        htype (str): Htype is how the default tensor metadata is defined.
        **kwargs: `htype` defaults can be overridden by passing any of the compatible parameters.
            To see all `htype`s and their correspondent arguments, check out `hub/htypes.py`.

    Raises:
        TensorAlreadyExistsError: If a tensor defined with `key` already exists.
    """

    if tensor_exists(key, storage):
        raise TensorAlreadyExistsError(key)

    TensorMeta.create(key, storage, htype=htype, **kwargs)
    IndexMeta.create(key, storage)


def add_samples_to_tensor(
    array: np.ndarray,
    key: str,
    storage: StorageProvider,
    batched: bool = False,
    tensor_meta: TensorMeta = None,
    index_meta: IndexMeta = None,
):
    """Adds samples to a tensor that already exists. `array` is chunked and sent to `storage`.
    For more on chunking, see the `generate_chunks` method.

    Args:
        array (np.ndarray): Array to be chunked/written. Batch axis (`array.shape[0]`) is optional, if `array` does
            have a batch axis, you should pass the argument `batched=True`.
        key (str): Key for where the chunks, index_meta, and meta will be located in `storage` relative to it's root.
        storage (StorageProvider): StorageProvider for storing the chunks, index_meta, and meta.
        batched (bool): If True, the provided `array`'s first axis (`shape[0]`) will be considered it's batch axis.
            If False, a new axis will be created with a size of 1 (`array.shape[0] == 1`). default=False
        tensor_meta (TensorMeta): Optionally provide a `TensorMeta`. If not provided, it will be loaded from `storage`.
        index_meta (IndexMeta): Optionally proivide an `IndexMeta`. If not provided, it will be loaded from `storage`.

    Raises:
        TensorDoesNotExistError: If a tensor at `key` does not exist. A tensor must be created first using
            `create_tensor(...)`.
    """

    if not tensor_exists(key, storage):
        raise TensorDoesNotExistError(key)

    if index_meta is None:
        index_meta = IndexMeta.load(key, storage)
    if tensor_meta is None:
        tensor_meta = TensorMeta.load(key, storage)

    array = normalize_and_batchify_array_shape(array, batched=batched)

    if len(tensor_meta.max_shape) <= 0:
        tensor_meta.update_tensor_meta_with_array(array, batched=True)

    _check_array_and_tensor_are_compatible(tensor_meta, array)

    # TODO: get the tobytes function from meta
    tobytes = row_wise_to_bytes

    array_length = array.shape[0]
    for i in range(array_length):
        sample = array[i]
        if 0 in sample.shape:
            # if sample has a 0 in the shape, no data will be written
            index_entry = {"chunk_names": []}  # type: ignore
        else:
            # TODO: we may want to call `tobytes` on `array` and call memoryview on that. this may depend on the access patterns we
            # choose to optimize for.
            b = memoryview(tobytes(sample))
            write_bytes(
                b,
                key,
                tensor_meta.chunk_size,
                storage,
                index_meta,
                extra_sample_meta={"shape": sample.shape},
            )

        tensor_meta.update_shape_interval(sample.shape)

    tensor_meta.length += array_length


def read_samples_from_tensor(
    key: str,
    storage: StorageProvider,
    index: Index = Index(),
    aslist: bool = False,
) -> Union[np.ndarray, List[np.ndarray]]:
    """Read (and unpack) samples from a tensor as an np.ndarray.

    Args:
        key (str): Key for where the chunks, index_meta, and meta are located in `storage` relative to it's root.
        storage (StorageProvider): StorageProvider for reading the chunks, index_meta, and meta.
        index (Index): Index that represents which samples to read.
        aslist (bool): If True, a list of np.ndarrays will be returned. Helpful for dynamic tensors.
            If False, a single np.ndarray will be returned unless the samples are dynamically shaped, in which case
            an error is raised.

    Raises:
        DynamicTensorNumpyError: If reading a dynamically-shaped array slice without `aslist=True`.
        NotImplementedError: Empty samples (shape contains 0) not implemented.

    Returns:
        np.ndarray: Array containing the sample(s) in the `array_slice` slice.
    """

    index_meta = IndexMeta.load(key, storage)
    tensor_meta = TensorMeta.load(key, storage)

    index_entries = [
        index_meta.entries[i] for i in index.values[0].indices(len(index_meta.entries))
    ]

    # TODO: read samples in parallel
    samples = []
    for i, index_entry in enumerate(index_entries):
        shape = index_entry["shape"]

        # check if all samples are the same shape
        last_shape = index_entries[i - 1]["shape"]
        if not aslist and shape != last_shape:
            raise DynamicTensorNumpyError(key, index)

        if 0 in shape:
            # TODO: implement support for 0s in shape and update docstring
            raise NotImplementedError("0s in shapes are not supported yet.")

        array = sample_from_index_entry(key, storage, index_entry, tensor_meta.dtype)
        samples.append(array)

    if aslist:
        if index.values[0].subscriptable():
            return samples
        else:
            return samples[0]

    return index.apply(np.array(samples))


def _check_array_and_tensor_are_compatible(tensor_meta: TensorMeta, array: np.ndarray):
    """An array is considered incompatible with a tensor if the `tensor_meta` entries don't match the `array` properties.
    Args:
        tensor_meta (dict): Tensor meta containing the expected properties of `array`.
        array (np.ndarray): Candidate array to check compatibility with `tensor_meta`.
    Raises:
        TensorMetaMismatchError: When `array` properties do not match the `tensor_meta`'s exactly. Also when
        `len(array.shape)` != len(tensor_meta max/min shapes).
        TensorInvalidSampleShapeError: All samples must have the same dimensionality (`len(sample.shape)`).
    """

    if tensor_meta.dtype != array.dtype.name:
        raise TensorMetaMismatchError("dtype", tensor_meta.dtype, array.dtype.name)

    sample_shape = array.shape[1:]

    expected_shape_len = len(tensor_meta.min_shape)
    actual_shape_len = len(sample_shape)
    if expected_shape_len != actual_shape_len:
        raise TensorInvalidSampleShapeError(
            "Sample shape length is expected to be {}, actual length is {}.".format(
                expected_shape_len, actual_shape_len
            ),
            sample_shape,
        )