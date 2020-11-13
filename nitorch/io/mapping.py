from abc import ABC, abstractmethod
from copy import copy
import torch
from nitorch.core.pyutils import make_list
from nitorch.spatial import affine_sub, affine_permute, voxel_size as affvx
from .indexing import expand_index, compose_index, is_droppedaxis, \
                      is_newaxis, is_sliceaxis, is_broadcastaxis, \
                      invert_permutation
from . import dtype as cast_dtype
from .readers import map


class MappedArray(ABC):
    """Base class for mapped arrays.

    Mapped arrays are usually stored on-disk, along with (diverse) metadata.

    They can be symbolically sliced, allowing for partial reading and
    (sometimes) writing of data from/to disk.
    Chaining of symbolic slicing operations is implemented in this base
    class. The actual partial io must be implemented by the child class.
    """

    fname: str = None             # filename (can be None if in-memory proxy)
    fileobj = None                # file-like object (`write`, `seek`, etc)
    is_compressed: bool = None    # is compressed
    dtype: torch.dtype = None     # on-disk data type
    slope: float = None           # intensity slope
    inter: float = None           # intensity shift

    affine = None                 # sliced voxel-to-world
    _affine = None                # original voxel-to-world
    spatial: tuple = None         # sliced spatial mask (len -> dim)
    _spatial: tuple = None        # original spatial mask (len -> _dim)
    shape: tuple = None           # sliced shape (len -> dim)
    _shape: tuple = None          # original shape (len -> _dim)
    slicer: tuple = None          # indexing into the parent
    permutation: tuple = None     # permutation of original dim (len -> _dim)

    dim = property(lambda self: len(self.shape))    # Nb of sliced dimensions
    _dim = property(lambda self: len(self._shape))  # Nb of original dimensions
    voxel_size = property(lambda self: affvx(self.affine))

    def __init__(self, **kwargs):
        self._init(**kwargs)

    def _init(self, **kwargs):
        for key, val in kwargs:
            setattr(self, key, val)

        if self.permutation is None:
            self.permutation = tuple(range(self._dim))

        if self.slicer is None:
            # same layout as on-disk
            self.spatial = self._spatial
            self.affine = self._affine
            self.shape = self._shape

        return self

    @classmethod
    def possible_extensions(cls):
        """List all possible extensions"""
        return tuple()

    def __getitem__(self, index):
        """Extract a sub-part of the array.

        Indices can only be slices, ellipses, lists or integers.
        Indices *into spatial dimensions* cannot be lists.

        Parameters
        ----------
        index : tuple[slice or ellipsis or int]

        Returns
        -------
        subarray : type(self)
            MappedArray object, with the indexing operations and affine
            matrix relating to the new sub-array.

        """
        return self.slice(index)

    def slice(self, index, new_shape=None, _pre_expanded=False):
        """Extract a sub-part of the array.

        Indices can only be slices, ellipses, lists or integers.
        Indices *into spatial dimensions* cannot be lists.

        Parameters
        ----------
        index : tuple[slice or ellipsis or int]
        new_shape : sequence[int], optional
            Output shape of the sliced object
        _pre_expanded : bool, default=False
            Set to True of `expand_index` has already been called on `index`

        Returns
        -------
        subarray : type(self)
            MappedArray object, with the indexing operations and affine
            matrix relating to the new sub-array.

        """
        if not _pre_expanded:
            index, new_shape = expand_index(index, self.shape)
        if any(isinstance(idx, list) for idx in index) > 1:
            raise ValueError('List indices not currently supported '
                             '(otherwise we enter advanced indexing '
                             'territory and it becomes too complicated).')
        new = copy(self)
        new.shape = new_shape

        # compute new affine
        if self.affine is not None:
            spatial_shape = [sz for sz, msk in zip(self.shape, self.spatial)
                             if msk]
            spatial_index = [idx for idx in index if isinstance(idx, slice)
                             or (isinstance(idx, int) and idx >= 0)]
            spatial_index = [idx for idx, msk in zip(spatial_index, self.spatial)
                             if msk]
            affine, _ = affine_sub(self.affine, spatial_shape, tuple(spatial_index))
        else:
            affine = None
        new.affine = affine

        # compute new slicer
        if self.slicer is None:
            new.slicer = index
        else:
            new.slicer = compose_index(self.slicer, index)

        # compute new spatial mask
        spatial = []
        i = 0
        for idx in index:
            if is_newaxis(idx):
                spatial.append(False)
            else:
                # original axis
                if not is_droppedaxis(idx):
                    spatial.append(self._spatial[self.permutation[i]])
                i += 1
        new.spatial = tuple(spatial)

        return new

    def __setitem__(self, index, value):
        """Write scaled data to disk.

        Parameters
        ----------
        index : tuple
            Tuple of indices (see `__getitem__`)
        value : array or tensor
            Array-like with shape `self[index].shape`

        Returns
        -------
        self : type(self)

        """
        if isinstance(value, MappedArray):
            raise NotImplementedError
        else:
            self.__getitem__(index).set_fdata(value)
        return self

    def __call__(self, *args, **kwargs):
        return self.fdata(*args, **kwargs)

    def permute(self, dims):
        """Permute dimensions

        Parameters
        ----------
        dims : sequence[int]
            A permutation of `range(self.dim)`

        Returns
        -------
        permarray : type(self)
            MappedArray object, with the indexing operations and affine
            matrix reflecting the permutation.

        """
        dims = list(dims)
        if len(dims) != self.dim or len(dims) != len(set(dims)):
            raise ValueError('there should be as many (unique) dimensions '
                             'as the array\'s dimension. Got {} and {}.'
                             .format(len(set(dims)), self.dim))

        # Permute tuples that relate to the current spatial dimensions
        shape = tuple(self.shape[d] for d in dims)
        spatial = tuple(self.spatial[d] for d in dims)

        # Permute tuples that relate to the slicer indices
        # (some of these slicers can drop dimensions, so their length
        #  can be greater than the current number of dimensions)
        slicer = []
        dim_map = []
        n_slicer = 0        # index into the slicer tuple
        n_dropped = 0       # number of dropped dimensions on the left
        for d in dims:
            if is_droppedaxis(self.slicer[n_slicer]):
                slicer.append(self.slicer[n_slicer])
                dim_map.append(self.permutation[n_slicer])
                n_dropped += 1
            else:
                slicer.append(self.slicer[d + n_dropped])
                dim_map.append(self.permutation[d + n_dropped])
            n_slicer += 1

        # permute affine
        # (it's a bit more complicated: we need to find the
        #  permutation of the *current* *spatial* dimensions)
        perm_spatial = [p for p in dims if self.spatial[p]]
        perm_spatial = sorted(range(len(perm_spatial)),
                              key=lambda k: perm_spatial[k])
        affine, _ = affine_permute(self.affine, self.shape, perm_spatial)

        # create new object
        new = copy(self)
        new.shape = shape
        new.spatial = spatial
        new.permutation = tuple(dim_map)
        new.slicer = tuple(slicer)
        new.affine = affine
        return new

    def transpose(self, dim0, dim1):
        """Transpose two dimensions

        Parameters
        ----------
        dim0 : int
            First dimension
        dim1 : int
        Second dimension

        Returns
        -------
        permarray : type(self)
            MappedArray object, with the indexing operations and affine
            matrix reflecting the transposition.

        """
        permutation = list(range(self.dim))
        permutation[dim0] = dim1
        permutation[dim1] = dim0
        return self.permute(permutation)

    @abstractmethod
    def data(self, dtype=None, device=None, casting='unsafe', rand=True,
             cutoff=None, dim=None, numpy=False):
        """Load the array in memory

        Parameters
        ----------
        dtype : type or torch.dtype or np.dtype, optional
            Output data type. By default, keep the on-disk data type.
        device : torch.device, default='cpu'
            Output device.
        rand : bool, default=False
            If the on-disk dtype is not floating point, sample noise
            in the uncertainty interval.
        cutoff : float or (float, float), default=(0, 1)
            Percentile cutoff. If only one value is provided, it is
            assumed to relate to the upper percentile.
        dim : int or list[int], optional
            Dimensions along which to compute percentiles.
            By default, they are computed on the flattened array.
        casting : {'no', 'equiv', 'safe', 'same_kind', 'unsafe', 'rescale'}, default='unsafe'
            Controls what kind of data casting may occur:
                * 'no': the data types should not be cast at all.
                * 'equiv': only byte-order changes are allowed.
                * 'safe': only casts which can preserve values are allowed.
                * 'same_kind': only safe casts or casts within a kind,
                  like float64 to float32, are allowed.
                * 'unsafe': any data conversions may be done.
                * 'rescale': the input data is rescaled to match the dynamic
                  range of the output type. The minimum value in the data
                  is mapped to the minimum value of the data type and the
                  maximum value in the data is mapped to the maximum value
                  of the data type.
                * 'rescale_zero': the input data is rescaled to match the
                  dynamic range of the output type, but ensuring that
                  zero maps to zero.
                  > If the data is signed and cast to a signed datatype,
                    zero maps to zero, and the scaling is chosen so that
                    both the maximum and minimum value in the data fit
                    in the output dynamic range.
                  > If the data is signed and cast to an unsigned datatype,
                    negative values "wrap around" (as with an unsafe cast).
                  > If the data is unsigned and cast to a signed datatype,
                    values are kept positive (the negative range is unused).
        numpy : bool, default=False
            Return a numpy array rather than a torch tensor.

        Returns
        -------
        dat : tensor[dtype]


        """
        pass

    def fdata(self, dtype=None, device=None, rand=False, cutoff=None,
              dim=None, numpy=False):
        """Load the scaled array in memory

        This function differs from `data` in several ways:
            * The output data type should be a floating point type.
            * If an affine scaling (slope, intercept) is defined in the
              file, it is applied to the data.
            * the default output data type is `torch.get_default_dtype()`.

        Parameters
        ----------
        dtype : dtype_like, optional
            Output data type. By default, use `torch.get_default_dtype()`.
            Should be a floating point type.
        device : torch.device, default='cpu'
            Output device.
        rand : bool, default=False
            If the on-disk dtype is not floating point, sample noise
            in the uncertainty interval.
        cutoff : float or (float, float), default=(0, 1)
            Percentile cutoff. If only one value is provided, it is
            assumed to relate to the upper percentile.
        dim : int or list[int], optional
            Dimensions along which to compute percentiles.
            By default, they are computed on the flattened array.
        numpy : bool, default=False
            Return a numpy array rather than a torch tensor.

        Returns
        -------
        dat : tensor[dtype]

        """
        # --- sanity check ---
        dtype = torch.get_default_dtype() if dtype is None else dtype
        info = cast_dtype.info(dtype)
        if not info['is_floating_point']:
            raise TypeError('Output data type should be a floating point '
                            'type but got {}.'.format(dtype))

        # --- get unscaled data ---
        dat = self.data(dtype=dtype, device=device, rand=rand,
                        cutoff=cutoff, dim=dim, numpy=numpy)

        # --- scale ---
        if self.slope != 1:
            dat *= self.slope
        if self.inter != 0:
            dat += self.inter

        return dat

    @abstractmethod
    def set_data(self, dat, casting='unsafe'):
        """Write (partial) data to disk.

        Parameters
        ----------
        dat : tensor
            Tensor to write on disk. It should have shape `self.shape`.
        casting : {'no', 'equiv', 'safe', 'same_kind', 'unsafe', 'rescale'}, default='unsafe'
            Controls what kind of data casting may occur:
                * 'no': the data types should not be cast at all.
                * 'equiv': only byte-order changes are allowed.
                * 'safe': only casts which can preserve values are allowed.
                * 'same_kind': only safe casts or casts within a kind,
                  like float64 to float32, are allowed.
                * 'unsafe': any data conversions may be done.
                * 'rescale': the input data is rescaled to match the dynamic
                  range of the output type. The minimum value in the data
                  is mapped to the minimum value of the data type and the
                  maximum value in the data is mapped to the maximum value
                  of the data type.
                * 'rescale_zero': the input data is rescaled to match the
                  dynamic range of the output type, but ensuring that
                  zero maps to zero.
                  > If the data is signed and cast to a signed datatype,
                    zero maps to zero, and the scaling is chosen so that
                    both the maximum and minimum value in the data fit
                    in the output dynamic range.
                  > If the data is signed and cast to an unsigned datatype,
                    negative values "wrap around" (as with an unsafe cast).
                  > If the data is unsigned and cast to a signed datatype,
                    values are kept positive (the negative range is unused).

        Returns
        -------
        self : type(self)

        """
        pass

    def set_fdata(self, dat):
        """Write (partial) scaled data to disk.

        Parameters
        ----------
        dat : tensor
            Tensor to write on disk. It should have shape `self.shape`
            and a floating point data type.

        Returns
        -------
        self : type(self)

        """
        # --- sanity check ---
        info = cast_dtype.info(dat.dtype)
        if not info['is_floating_point']:
            raise TypeError('Input data type should be a floating point '
                            'type but got {}.'.format(dat.dtype))
        if dat.shape != self.shape:
            raise TypeError('Expected input shape {} but got {}.'
                            .format(self.shape, dat.shape))

        # --- detach ---
        if torch.is_tensor(dat):
            dat = dat.detach()

        # --- unscale ---
        if self.inter != 0 or self.slope != 1:
            dat = dat.clone() if torch.is_tensor(dat) else dat.copy()
        if self.inter != 0:
            dat -= self.inter
        if self.slope != 1:
            dat /= self.slope

        # --- set unscaled data ---
        self.set_data(dat)

        return self

    @abstractmethod
    def metadata(self, keys=None):
        """Read metadata

        .. note:: The values returned by this function always relate to
                  the full volume, even if we're inside a view. That is,
                  we always return the affine of the original volume.
                  To get an affine matrix that relates to the view,
                  use `self.affine`.

        Parameters
        ----------
        keys : sequence[str], optional
            List of metadata to load. They can either be one of the
            generic metadata keys define in `io.metadata`, or a
            format-specific metadata key.
            By default, all generic keys that are found in the file
            are returned.

        Returns
        -------
        metadata : dict
            A dictionary of metadata

        """
        pass

    @abstractmethod
    def set_metadata(self, **meta):
        """Write metadata

        Parameters
        ----------
        meta : dict, optional
            Dictionary of metadata.
            Fields that are absent from the dictionary or that have
            value `None` are kept untouched.

        Returns
        -------
        self : type(self)

        """
        pass

    def unsqueeze(self, dim):
        """Add a dimension of size 1 in position `dim`.

        Parameters
        ----------
        dim : int
            The dimension is added to the right of `dim` if `dim < 0`
            else it is added to the left of `dim`.

        Returns
        -------
        MappedArray

        """
        index = [slice(None)] * self.dim
        if dim < 0:
            dim = self.dim + dim
        index = index[:dim] + [None] + index[dim:]
        return self[tuple(index)]

    def squeeze(self, dim):
        """Remove all dimensions of size 1.

        Parameters
        ----------
        dim : int or sequence[int], optional
            If provided, only this dimension is squeezed. It *must* be a
            dimension of size 1.

        Returns
        -------
        MappedArray

        """
        if dim is None:
            dim = [d for d in range(self.dim) if self.shape[d] == 1]
        dim = make_list(dim)
        if any(self.shape[d] != 1 for d in dim):
            raise ValueError('Impossible to squeeze non-singleton dimensions.')
        index = [slice(None) if d not in dim else 0 for d in range(self.dim)]
        return self[tuple(index)]

    def unbind(self, dim=0, keepdim=False):
        """Extract all arrays along dimension `dim` and drop that dimension.

        Parameters
        ----------
        dim : int, default=0
            Dimension along which to unstack.
        keepdim : bool, default=False
            Do not drop the unstacked dimension.

        Returns
        -------
        list[MappedArray]

        """
        index = [slice(None)] * self.dim
        if keepdim:
            index = index[:dim+1] + [None] + index[dim+1:]
        out = []
        for i in range(self.shape[dim]):
            index[dim] = i
            out.append(self[tuple(index)])
        return out

    def chunk(self, chunks, dim=0):
        """Split the array into smaller arrays of size `chunk` along `dim`.

        Parameters
        ----------
        chunks : int
            Number of chunks.
        dim : int, default=0
            Dimensions along which to split.

        Returns
        -------
        list[MappedArray]

        """
        index = [slice(None)] * self.dim
        out = []
        for i in range(self.shape[dim]):
            index[dim] = slice(i*chunks, (i+1)*chunks)
            out.append(self[tuple(index)])
        return out

    def split(self, chunks, dim=0):
        """Split the array into smaller arrays along `dim`.

        Parameters
        ----------
        chunks : int or list[int]
            If `int`: Number of chunks (see `self.chunk`)
            Else: Size of each chunk. Must sum to `self.shape[dim]`.
        dim : int, default=0
            Dimensions along which to split.

        Returns
        -------
        list[MappedArray]

        """
        if isinstance(chunks, int):
            return self.chunk(chunks, dim)
        chunks = make_list(chunks)
        if sum(chunks) != self.shape[dim]:
            raise ValueError('Chunks must cover the full dimension. '
                             'Got {} and {}.'
                             .format(sum(chunks), self.shape[dim]))
        index = [slice(None)] * self.dim
        previous_chunks = 0
        out = []
        for chunk in chunks:
            index[dim] = slice(previous_chunks, previous_chunks+chunk)
            out.append(self[tuple(index)])
            previous_chunks += chunk
        return out


class CatArray(MappedArray):
    """A concatenation of mapped arrays.

    This is largely inspired by virtual concatenation of file_array in
    SPM: https://github.com/spm/spm12/blob/master/@file_array/cat.m

    """

    _arrays: tuple = []
    _dim_cat: int = None

    # defer attributes
    fname = property(lambda self: tuple(a.fname for a in self._arrays))
    fileobj = property(lambda self: tuple(a.fileobj for a in self._arrays))
    is_compressed = property(lambda self: tuple(a.is_compressed for a in self._arrays))
    dtype = property(lambda self: tuple(a.dtype for a in self._arrays))
    slope = property(lambda self: tuple(a.slope for a in self._arrays))
    inter = property(lambda self: tuple(a.inter for a in self._arrays))
    _shape = property(lambda self: tuple(a._shape for a in self._arrays))
    _dim = property(lambda self: tuple(a._dim for a in self._arrays))
    affine = property(lambda self: tuple(a.affine for a in self._arrays))
    _affine = property(lambda self: tuple(a._affine for a in self._arrays))
    spatial = property(lambda self: tuple(a.spatial for a in self._arrays))
    _spatial = property(lambda self: tuple(a._spatial for a in self._arrays))
    slicer = property(lambda self: tuple(a.slicer for a in self._arrays))
    permutation = property(lambda self: tuple(a.permutation for a in self._arrays))
    voxel_size = property(lambda self: tuple(a.voxel_size for a in self._arrays))

    def __init__(self, arrays, dim=0):
        """

        Parameters
        ----------
        arrays : sequence[MappedArray]
            Arrays to concatenate. Their shapes should be identical
            except along dimension `dim`.
        dim : int, default=0
            Dimension along white to concatenate the arrays
        """
        super().__init__()

        arrays = list(arrays)
        dim = dim or 0
        self._dim_cat = dim

        # sanity checks
        shapes = []
        for i, array in enumerate(arrays):
            if not isinstance(array, MappedArray):
                arrays[i] = map(array)
            shape = list(array.shape)
            del shape[dim]
            shapes.append(shape)
        shape0, *shapes = shapes
        if not all(shape == shape0 for shape in shapes):
            raise ValueError('Shapes of all concatenated arrays should '
                             'be equal except in the concatenation dimension.')

        # compute output shape
        shape = list(arrays[0].shape)
        dims = [array.shape[dim] for array in arrays]
        shape[dim] = sum(dims)
        self.shape = tuple(shape)

        # concatenate
        _arrays = []
        for i, array in enumerate(arrays):
            _arrays.append(array)
        self._arrays = tuple(_arrays)

    def slice(self, index, new_shape=None, _pre_expanded=False):
        # overload slicer -> slice individual arrays
        if not _pre_expanded:
            index, new_shape = expand_index(index, self.shape)
        assert len(index) > 0, "index should never be empty here"
        if any(isinstance(idx, list) for idx in index) > 1:
            raise ValueError('List indices not currently supported '
                             '(otherwise we enter advanced indexing '
                             'territory and it becomes too complicated).')
        index = list(index)
        shape_cat = self.shape[self._dim_cat]

        # find out which index corresponds to the concatenated dimension
        # + compute the concatenated dimension in the output array
        new_dim_cat = self._dim_cat
        nb_old_dim = -1
        for map_dim_cat, idx in enumerate(index):
            if is_newaxis(idx):
                # an axis was added: dim_cat moves to the right
                new_dim_cat = new_dim_cat + 1
            elif is_droppedaxis(idx):
                # an axis was dropped: dim_cat moves to the left
                new_dim_cat = new_dim_cat - 1
                nb_old_dim += 1
            else:
                nb_old_dim += 1
            if nb_old_dim >= self._dim_cat:
                # found the concatenated dimension
                break
        index_cat = index[map_dim_cat]

        if is_droppedaxis(index_cat):
            # if the concatenated dimension is dropped, return the
            # corresponding array (sliced)
            nb_pre = 0
            for i in range(len(self._arrays)):
                if nb_pre < index_cat:
                    nb_pre += self._arrays[i].shape[self._dim_cat]
                    continue
                if i > index_cat:
                    # we've passed the volume
                    i = i - 1
                    nb_pre -= self._arrays[i].shape[self._dim_cat]
                index_cat = index_cat - nb_pre
                index[map_dim_cat] = index_cat
                # slice the array but set `_pre_expanded=True`
                return self._arrays[i].slice(tuple(index), new_shape, True)

        # else, we may have to drop some volumes and slice the others
        assert is_sliceaxis(index_cat), "This should not happen"
        arrays = self._arrays

        if index_cat.step < 0:
            # if negative step, invert everything and update index_cat
            invert_index = [slice(None)] * self.dim
            invert_index[self._dim_cat] = slice(None, None, -1)
            arrays = [array[tuple(invert_index)] for array in arrays]
            index_cat = slice(shape_cat - index_cat.start,
                              shape_cat - index_cat.stop,
                              -index_cat.step)

        nb_pre = 0
        kept_arrays = []
        starts = []
        stops = []
        size_since_start = 0
        while len(arrays) > 0:
            # pop array
            array, *arrays = arrays
            size_cat = array.shape[self._dim_cat]
            if nb_pre + size_cat < index_cat.start:
                # discarded volumes at the beginning
                nb_pre += size_cat
                continue
            if nb_pre < index_cat.start:
                # first volume
                kept_arrays.append(array)
                starts.append(index_cat.start - nb_pre)
            elif nb_pre < index_cat.stop:
                # other kept volume
                kept_arrays.append(array)
                skip = size_since_start - (size_since_start // index_cat.step) * index_cat.step
                starts.append(skip)
            # compute stopping point
            nb_elem_total = (index_cat.stop - index_cat.start) // index_cat.step
            nb_elem_prev = size_since_start // index_cat.step
            nb_elem_remaining = nb_elem_total - nb_elem_prev
            nb_elem_this_volume = (size_cat - starts[-1]) // index_cat.step
            if nb_elem_this_volume <= nb_elem_remaining:
                # last volume
                stops.append(index_cat.stop - nb_pre)
                break
            # read as much as possible
            size_since_start += size_cat
            nb_pre += size_cat
            stops.append(None)
            continue

        # slice kept arrays
        arrays = []
        for array, start, stop in zip(kept_arrays, starts, stops):
            index[map_dim_cat] = slice(start, stop, index_cat.step)
            arrays.append(array[tuple(index)])

        # create new CatArray
        new = copy(self)
        new._arrays = arrays
        new._dim_cat = new_dim_cat
        return new

    def permute(self, dims):
        # overload permutation -> permute individual arrays
        new = copy(self)
        new._arrays = [array.permute(dims) for array in new._arrays]
        iperm = invert_permutation(dims)
        new._dim_cat = iperm[new._dim_cat]
        return new

    def data(self, *args, numpy=False, **kwargs):
        # read individual arrays and concatenate them
        # TODO: it would be more efficient to preallocate the whole
        #   array and pass the appropriate buffer to each reader but
        #   (1) we don't have the option to provide a buffer yet
        #   (2) everything's already quite inefficient

        dats = [torch.as_tensor(array.data(*args, **kwargs))
                for array in self._arrays]
        dat = torch.cat(dats, dim=self._dim_cat)
        if numpy:
            dat = dat.numpy()
        return dat

    def fdata(self, *args, numpy=False, **kwargs):
        # read individual arrays and concatenate them
        # TODO: it would be more efficient to preallocate the whole
        #   array and pass the appropriate buffer to each reader but
        #   (1) we don't have the option to provide a buffer yet
        #   (2) everything's already quite inefficient

        dats = [array.fdata(*args, **kwargs) for array in self._arrays]
        dat = torch.cat(dats, dim=self._dim_cat)
        if numpy:
            dat = dat.numpy()
        return dat

    def set_data(self, dat, *args, **kwargs):
        # slice the input data and write it into each array
        size_prev = 0
        index = [None] * self.dim
        for array in self._arrays:
            size_cat = array.shape[self._dim_cat]
            index[self._dim_cat] = slice(size_prev, size_prev + size_cat)
            array._set_data(dat[tuple(index)], *args, **kwargs)

    def set_fdata(self, dat, *args, **kwargs):
        # slice the input data and write it into each array
        size_prev = 0
        index = [None] * self.dim
        for array in self._arrays:
            size_cat = array.shape[self._dim_cat]
            index[self._dim_cat] = slice(size_prev, size_prev + size_cat)
            array._set_fdata(dat[tuple(index)], *args, **kwargs)

    def metadata(self, *args, **kwargs):
        return tuple(array.metadata(*args, **kwargs) for array in self._arrays)

    def set_metadata(self, **meta):
        raise NotImplementedError('Cannot write metadata into concatenated '
                                  'array')


def cat(arrays, dim=0):
    """Concatenate mapped arrays along a dimension.

    Parameters
    ----------
        arrays : sequence[MappedArray]
            Arrays to concatenate. Their shapes should be identical
            except along dimension `dim`.
        dim : int, default=0
            Dimension along white to concatenate the arrays

    Returns
    -------
    CatArray
        A symbolic concatenation of all input arrays.
        Its shape along dimension `dim` is the sum of all input shapes
        along dimension `dim`.
    """
    return CatArray(arrays, dim)


def stack(arrays, dim=0):
    """Stack mapped arrays along a dimension.

    Parameters
    ----------
        arrays : sequence[MappedArray]
            Arrays to concatenate. Their shapes should be identical
            except along dimension `dim`.
        dim : int, default=0
            Dimension along white to concatenate the arrays

    Returns
    -------
    CatArray
        A symbolic stack of all input arrays.

    """
    arrays = [arrays.unsqueeze(array, dim=dim) for array in arrays]
    return cat(arrays, dim=dim)
