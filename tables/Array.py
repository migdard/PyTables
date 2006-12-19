########################################################################
#
#       License: BSD
#       Created: October 10, 2002
#       Author:  Francesc Altet - faltet@carabos.com
#
#       $Id$
#
########################################################################

"""Here is defined the Array class.

See Array class docstring for more info.

Classes:

    Array
    ImageArray

Functions:


Misc variables:

    __version__


"""

import types, warnings, sys

import numpy

try:
    import Numeric
    Numeric_imported = True
except ImportError:
    Numeric_imported = False

try:
    import numarray
    import numarray.strings
    numarray_imported = True
except ImportError:
    numarray_imported = False

import tables.hdf5Extension as hdf5Extension
from tables.utils import processRange, processRangeRead, convToFlavor, \
     convToNP, is_idx, byteorders
from tables.atom import split_type
from tables.Leaf import Leaf, Filters


__version__ = "$Revision$"


# default version for ARRAY objects
#obversion = "1.0"    # initial version
#obversion = "2.0"    # Added an optional EXTDIM attribute
#obversion = "2.1"    # Added support for complex datatypes
#obversion = "2.2"    # This adds support for time datatypes.
obversion = "2.3"    # This adds support for enumerated datatypes.


class Array(hdf5Extension.Array, Leaf):
    """
    This class represents homogeneous datasets in an HDF5 file.

    This class provides methods to write or read data to or from array
    objects in the file.  This class does not allow you to enlarge the
    datasets on disk; use the `EArray` class if you want enlargeable
    dataset support or compression features, or `CArray` if you just
    want compression.

    An interesting property of the `Array` class is that it remembers
    the *flavor* of the object that has been saved so that if you
    saved, for example, a ``list``, you will get a ``list`` during
    readings afterwards; if you saved a NumPy array, you will get a
    NumPy object, and so forth.

    Note that this class inherits all the public attributes and
    methods that `Leaf` already provides.  However, as `Array`
    instances have no internal I/O buffers, it is not necessary to use
    the ``flush()`` method they inherit from `Leaf` in order to save
    their internal state to disk.  When a writing method call returns,
    all the data is already on disk.

    Instance variables (specific of `Array`):

    `flavor`
        The representation of data read from this array.  It can be
        any of 'numpy', 'numarray', 'numeric' or 'python' values.
    `nrows`
        The length of the main dimension of the array.
    `nrow`
        On iterators, this is the index of the current row.
    `maindim`
        The dimension along which iterators work.  Its value is 0
        (i.e. the first dimension) when the dataset is not extendable,
        and ``self.extdim`` (where available) for extendable ones.
    `dtype`
        The NumPy type of the represented array.
    `type`
        The PyTables type of the represented array.
    `itemsize`
        The size in bytes of an item in the array.
    `byteorder`
        The byte ordering of the items in the array on disk.
    """

    # Class identifier.
    _c_classId = 'ARRAY'


    # Properties
    # ~~~~~~~~~~
    byteorder = property(
        lambda self: byteorders[self.dtype.byteorder], None, None,
        "The endianness of data in memory ('big', 'little' or 'irrelevant').")

    itemsize = property(
        lambda self: self.dtype.itemsize, None, None,
        "The size of the base items (shortcut for self.dtype.itemsize).")

    def _getnrows(self):
        if self.shape == ():
            return 1  # scalar case
        else:
            return self.shape[self.maindim]
    nrows = property(
        _getnrows, None, None,
        "The length of the main dimension of the array.")

    def _getrowsize(self):
        maindim = self.maindim
        rowsize = self.itemsize
        for i, dim in enumerate(self.shape):
            if i != maindim:
                rowsize *= dim
        return rowsize
    rowsize = property(
        _getrowsize, None, None,
        "The size of the rows in dimensions orthogonal to maindim.")

    # Other methods
    # ~~~~~~~~~~~~~
    def __init__(self, parentNode, name,
                 object=None, title="",
                 _log=True):
        """
        Create an `Array` instance.

        Keyword arguments:

        `object`
            The object to be saved.  It can be any object of one of
            NumPy, numarray, Numeric, list, tuple, string, integer of
            floating point types, provided that it is regular
            (i.e. not like ``[[1,2], 2]``).
        `title`
            Sets a ``TITLE`` attribute on the array entity.
        `flavor`
            Sets the representation of data read from this array.
        """

        self._v_version = None
        """The object version of this array."""

        self._v_new = new = object is not None
        """Is this the first time the node has been created?"""
        self._v_new_title = title
        """New title for this node."""

        self._object = object
        """
        The object to be stored in the array.  It can be any of
        ``numpy``, ``numarray``, ``numeric``, list, tuple, string,
        integer of floating point types, provided that they are
        regular (i.e. they are not like ``[[1, 2], 2]``).
        """
        self._v_nrowsinbuf = None
        """The maximum number of rows that are read on each chunk iterator."""
        self._v_chunkshape = None
        """
        The HDF5 chunk size for ``CArray``, ``EArray`` and ``VLArray``
        objects.
        """
        self._v_convert = True
        """Whether the ``Array`` object must be converted or not."""
        self._enum = None
        """The enumerated type containing the values in this array."""

        # Miscellaneous iteration rubbish.
        self._start = None
        """Starting row for the current iteration."""
        self._stop = None
        """Stopping row for the current iteration."""
        self._step = None
        """Step size for the current iteration."""
        self._nrowsread = None
        """Number of rows read up to the current state of iteration."""
        self._startb = None
        """Starting row for current buffer."""
        self._stopb = None
        """Stopping row for current buffer. """
        self._row = None
        """Current row in iterators (sentinel)."""
        self._init = False
        """Whether we are in the middle of an iteration or not (sentinel)."""
        self.listarr = None
        """Current buffer in iterators."""

        # Documented (*public*) attributes.
        self.shape = None
        """The shape of the stored array."""
        self.flavor = None
        """The object representation of this array.  It can be any of
        'numpy', 'numarray', 'numeric' or 'python' values."""
        self.nrow = None
        """On iterators, this is the index of the current row."""
        self.dtype = None
        """The NumPy type of the represented array."""
        self.type = None
        """The PyTables type of the represented array."""
        self.extdim = -1   # ordinary arrays are not enlargeable
        """The index of the enlargeable dimension."""

        # Ordinary arrays have no filters: leaf is created with default ones.
        super(Array, self).__init__(parentNode, name, new, Filters(), _log)


    def _g_create(self):
        """Save a new array in file."""

        self._v_version = obversion
        try:
            nparr, self.flavor = convToNP(self._object)
        except:  #XXX
            # Problems converting data. Close the node and re-raise exception.
            #print "Problems converting input object:", str(self._object)
            self.close(flush=0)
            raise

        # Raise an error in case of unsupported object
        if nparr.dtype.kind in ['V', 'U', 'O']:  # in void, unicode, object
            raise TypeError, \
"Array objects cannot currently deal with void, unicode or object arrays"

        # Decrease the number of references to the object
        self._object = None

        # The shape of this array
        self.shape = nparr.shape

        # Create the array on-disk
        try:
            (self._v_objectID, self.dtype, self.type) = (
                self._createArray(nparr, self._v_new_title))
        except:  #XXX
            # Problems creating the Array on disk. Close node and re-raise.
            self.close(flush=0)
            raise

        # Compute the optimal buffer size (nrowsinbuf)
        chunkshape = self._calc_chunkshape(self.nrows, self.rowsize)
        self._v_nrowsinbuf = self._calc_nrowsinbuf(chunkshape, self.rowsize)

        return self._v_objectID


    def _g_open(self):
        """Get the metadata info for an array in file."""

        (self._v_objectID, self.dtype, self.type, self.shape,
         self.flavor, self._v_chunkshape) = self._openArray()

        # Get enumeration from disk.
        if split_type(self.type)[0] == 'enum':
            (self._enum, self.dtype) = self._g_loadEnum()

        # Compute the optimal buffer size (nrowsinbuf)
        chunkshape = self._calc_chunkshape(self.nrows, self.rowsize)
        self._v_nrowsinbuf = self._calc_nrowsinbuf(chunkshape, self.rowsize)

        return self._v_objectID


    def getEnum(self):
        """
        Get the enumerated type associated with this array.

        If this array is of an enumerated type, the corresponding `Enum`
        instance is returned.  If it is not of an enumerated type, a
        ``TypeError`` is raised.
        """

        if split_type(self.type)[0] != 'enum':
            raise TypeError("array ``%s`` is not of an enumerated type"
                            % self._v_pathname)

        return self._enum


    def iterrows(self, start=None, stop=None, step=None):
        """Iterate over all the rows or a range.

        """

        try:
            (self._start, self._stop, self._step) = \
                          processRangeRead(self.nrows, start, stop, step)
        except IndexError:
            # If problems with indexes, silently return the null tuple
            return ()
        self._initLoop()
        return self


    def __iter__(self):
        """Iterate over all the rows."""

        if not self._init:
            # If the iterator is called directly, assign default variables
            self._start = 0
            self._stop = self.nrows
            self._step = 1
            # and initialize the loop
            self._initLoop()
        return self


    def _initLoop(self):
        "Initialization for the __iter__ iterator"

        self._nrowsread = self._start
        self._startb = self._start
        self._row = -1   # Sentinel
        self._init = True  # Sentinel
        self.nrow = self._start - self._step    # row number


    def next(self):
        "next() method for __iter__() that is called on each iteration"
        if self._nrowsread >= self._stop:
            self._init = False
            raise StopIteration        # end of iteration
        else:
            # Read a chunk of rows
            if self._row+1 >= self._v_nrowsinbuf or self._row < 0:
                self._stopb = self._startb+self._step*self._v_nrowsinbuf
                # Protection for reading more elements than needed
                if self._stopb > self._stop:
                    self._stopb = self._stop
                self.listarr = self.read(self._startb, self._stopb, self._step)
                # Swap the axes to easy the return of elements
                if self.extdim > 0:
                    if self.flavor == "numarray":
                        if numarray_imported:
                            self.listarr = numarray.swapaxes(self.listarr,
                                                             self.extdim, 0)
                        else:
                            # Warn the user
                            warnings.warn( \
"""The object on-disk has numarray flavor, but numarray is not installed locally. Returning a NumPy object instead!.""")
                            # Default to NumPy
                            self.listarr = numpy.swapaxes(self.listarr,
                                                          self.extdim, 0)
                    elif self.flavor == "numeric":
                        if Numeric_imported:
                            self.listarr = Numeric.swapaxes(self.listarr,
                                                            self.extdim, 0)
                        else:
                            # Warn the user
                            warnings.warn( \
"""The object on-disk has numeric flavor, but Numeric is not installed locally. Returning a NumPy object instead!.""")
                            # Default to NumPy
                            self.listarr = numpy.swapaxes(self.listarr,
                                                          self.extdim, 0)
                    else:
                        self.listarr = numpy.swapaxes(self.listarr,
                                                      self.extdim, 0)
                self._row = -1
                self._startb = self._stopb
            self._row += 1
            self.nrow += self._step
            self._nrowsread += self._step
            # Fixes bug #968132
            #if self.listarr.shape:
            if self.shape:
                return self.listarr[self._row]
            else:
                return self.listarr    # Scalar case


    def _interpret_indexing(self, keys):
        """Internal routine used by __getitem__ and __setitem__"""

        maxlen = len(self.shape)
        shape = (maxlen,)
        startl = numpy.empty(shape=shape, dtype=numpy.int64)
        stopl = numpy.empty(shape=shape, dtype=numpy.int64)
        stepl = numpy.empty(shape=shape, dtype=numpy.int64)
        stop_None = numpy.zeros(shape=shape, dtype=numpy.int64)
        if not isinstance(keys, tuple):
            keys = (keys,)
        nkeys = len(keys)
        dim = 0
        # Here is some problem when dealing with [...,...] params
        # but this is a bit weird way to pass parameters anyway
        for key in keys:
            ellipsis = 0  # Sentinel
            if isinstance(key, types.EllipsisType):
                ellipsis = 1
                for diml in xrange(dim, len(self.shape) - (nkeys - dim) + 1):
                    startl[dim] = 0
                    stopl[dim] = self.shape[diml]
                    stepl[dim] = 1
                    dim += 1
            elif dim >= maxlen:
                raise IndexError, "Too many indices for object '%s'" % \
                      self._v_pathname
            elif is_idx(key):
                # Protection for index out of range
                if key >= self.shape[dim]:
                    raise IndexError, "Index out of range"
                if key < 0:
                    # To support negative values (Fixes bug #968149)
                    key += self.shape[dim]
                start, stop, step = processRange(self.shape[dim],
                                                 key, key+1, 1)
                stop_None[dim] = 1
            elif isinstance(key, slice):
                start, stop, step = processRange(self.shape[dim],
                                                 key.start, key.stop, key.step)
            else:
                raise TypeError, "Non-valid index or slice: %s" % \
                      key
            if not ellipsis:
                startl[dim] = start
                stopl[dim] = stop
                stepl[dim] = step
                dim += 1

        # Complete the other dimensions, if needed
        if dim < len(self.shape):
            for diml in xrange(dim, len(self.shape)):
                startl[dim] = 0
                stopl[dim] = self.shape[diml]
                stepl[dim] = 1
                dim += 1

        # Compute the shape for the container properly. Fixes #1288792
        shape = []
        for dim in xrange(len(self.shape)):
            # The negative division operates differently with python scalars
            # and numpy scalars (which are similar to C conventions). See:
            # http://www.python.org/doc/faq/programming.html#why-does-22-10-return-3
            # and
            # http://www.peterbe.com/Integer-division-in-programming-languages
            # for more info on this issue.
            # I've finally decided to rely on the len(xrange) function.
            # F. Altet 2006-09-25
            #new_dim = ((stopl[dim] - startl[dim] - 1) / stepl[dim]) + 1
            new_dim = len(xrange(startl[dim], stopl[dim], stepl[dim]))
            if not (new_dim == 1 and stop_None[dim]):
            #if not stop_None[dim]:
                # Append dimension
                shape.append(new_dim)

        return startl, stopl, stepl, shape


    def __getitem__(self, keys):
        """Returns an Array element, row or extended slice.

        It takes different actions depending on the type of the "keys"
        parameter:

        If "keys" is an integer, the corresponding row is returned. If
        "keys" is a slice, the row slice determined by key is returned.

        """

        startl, stopl, stepl, shape = self._interpret_indexing(keys)
        return self._readSlice(startl, stopl, stepl, shape)


    def __setitem__(self, keys, value):
        """Sets an Array element, row or extended slice.

        It takes different actions depending on the type of the "key"
        parameter:

        If "key" is an integer, the corresponding row is assigned to
        value.

        If "key" is a slice, the row slice determined by it is
        assigned to "value". If needed, this "value" is broadcasted to
        fit in the desired range. If the slice to be updated exceeds
        the actual shape of the array, only the values in the existing
        range are updated, i.e. the index error will be silently
        ignored. If "value" is a multidimensional object, then its
        shape must be compatible with the slice specified in "key",
        otherwhise, a ValueError will be issued.

        """

        startl, stopl, stepl, shape = self._interpret_indexing(keys)
        countl = ((stopl - startl - 1) / stepl) + 1
        # Create an array compliant with the specified slice
        narr = numpy.empty(shape=shape, dtype=self.dtype)
        # Set the same byteorder than on-disk (if well-defined)
        if self.byteorder in ['little', 'big']:
            # Note that .newbyteorder() doesn't make a data copy
            narr = narr.newbyteorder(self.byteorder)

        # Assign the value to it
        try:
            narr[...] = value
        except Exception, exc:  #XXX
            raise ValueError, \
"""value parameter '%s' cannot be converted into an array object compliant with %s:
'%r'
The error was: <%s>""" % (value, self.__class__.__name__, self, exc)

        if narr.size:
            self._modify(startl, stepl, countl, narr)


    # Accessor for the _readArray method in superclass
    def _readSlice(self, startl, stopl, stepl, shape):

        # Create the container for the slice
        arr = numpy.empty(dtype=self.dtype, shape=shape)

        # Protection against reading empty arrays
        if 0 not in shape:
            # Arrays that have non-zero dimensionality
            self._g_readSlice(startl, stopl, stepl, arr)

        if not self._v_convert:
            return arr

        if self.flavor == 'numpy':
            if arr.shape == ():  # Scalar case
                return arr[()]  # Return a numpy scalar
            else:             # No conversion needed
                return arr
        # Fixes #968131
        elif arr.shape == ():  # Scalar case
            return arr.item()  # return the python value
        else:
            return convToFlavor(arr, self.flavor)


    def read(self, start=None, stop=None, step=None):
        """Read the array from disk and return it as a self.flavor object."""

        maindim = self.maindim

        (start, stop, step) = processRangeRead(self.nrows, start, stop, step)
        #rowstoread = ((stop - start - 1) / step) + 1
        rowstoread = len(xrange(start, stop, step))
        shape = list(self.shape)
        if shape:
            shape[maindim] = rowstoread
        arr = numpy.empty(dtype=self.dtype, shape=shape)
        # Set the correct byteorder for this array
        arr.dtype = arr.dtype.newbyteorder(self.byteorder)

        # Protection against reading empty arrays
        if 0 not in shape:
            # Arrays that have non-zero dimensionality
            self._readArray(start, stop, step, arr)

        if self.flavor == "numpy":
            # No conversion needed
            return arr
        # Fixes #968131
        elif arr.shape == ():  # Scalar case and flavor is not 'numpy'
            return arr.item()  # return the python value
        else:
            return convToFlavor(arr, self.flavor)


    def _g_copyWithStats(self, group, name, start, stop, step,
                         title, filters, _log):
        "Private part of Leaf.copy() for each kind of leaf"
        # Get the slice of the array
        # (non-buffered version)
        if self.shape:
            arr = self[start:stop:step]
        else:
            arr = self[()]
        # Build the new Array object
        object = Array(group, name, arr, title=title, _log=_log)
        nbytes = self.itemsize
        for i in self.shape:
            nbytes*=i

        return (object, nbytes)


    def __repr__(self):
        """This provides more metainfo in addition to standard __str__"""

        return """%s
  type := %r
  shape := %r
  maindim := %r
  flavor := %r
  byteorder := %r""" % (self, self.type, self.shape, self.maindim,
                        self.flavor, self.byteorder)



class ImageArray(Array):

    """
    Array containing an image.

    This class has no additional behaviour or functionality compared
    to that of an ordinary array.  It simply enables the user to open
    an ``IMAGE`` HDF5 node as a normal `Array` node in PyTables.
    """

    # Class identifier.
    _c_classId = 'IMAGE'
