########################################################################
#
#       License: BSD
#       Created: June 08, 2004
#       Author:  Francesc Altet - faltet@carabos.com
#
#       $Id$
#
########################################################################

"""Here is defined the Index class.

See Index class docstring for more info.

Classes:

    IndexProps
    Index

Functions:


Misc variables:

    __version__


"""

import math
import bisect
from time import time, clock
import os, os.path
import tempfile

import numpy

from tables import indexesExtension
from tables import utilsExtension
from tables.file import openFile
from tables.attributeset import AttributeSet
from tables.node import NotLoggedMixin
from tables.atom import Int64Atom, Atom
from tables.earray import EArray
from tables.carray import CArray
from tables.leaf import Filters
from tables.indexes import CacheArray, LastRowArray, IndexArray
from tables.group import Group
from tables.path import joinPath
from tables.constants import LIMBOUNDS_MAX_SLOTS, LIMBOUNDS_MAX_SIZE

from tables.lrucacheExtension import ObjectCache


__version__ = "$Revision: 1236 $"

# Python implementations of NextAfter and NextAfterF
#
# These implementations exist because the standard function
# nextafterf is not available on Microsoft platforms.
#
# These implementations are based on the IEEE representation of
# floats and doubles.
# Author:  Shack Toms - shack@livedata.com
#
# Thanks to Shack Toms shack@livedata.com for NextAfter and NextAfterF
# implementations in Python. 2004-10-01

epsilon  = math.ldexp(1.0, -53) # smallest double such that 0.5+epsilon != 0.5
epsilonF = math.ldexp(1.0, -24) # smallest float such that 0.5+epsilonF != 0.5

maxFloat = float(2**1024 - 2**971)  # From the IEEE 754 standard
maxFloatF = float(2**128 - 2**104)  # From the IEEE 754 standard

minFloat  = math.ldexp(1.0, -1022) # min positive normalized double
minFloatF = math.ldexp(1.0, -126)  # min positive normalized float

smallEpsilon  = math.ldexp(1.0, -1074) # smallest increment for doubles < minFloat
smallEpsilonF = math.ldexp(1.0, -149)  # smallest increment for floats < minFloatF

infinity = math.ldexp(1.0, 1023) * 2
infinityF = math.ldexp(1.0, 128)
#Finf = float("inf")  # Infinite in the IEEE 754 standard (not avail in Win)

# A portable representation of NaN
# if sys.byteorder == "little":
#     testNaN = struct.unpack("d", '\x01\x00\x00\x00\x00\x00\xf0\x7f')[0]
# elif sys.byteorder == "big":
#     testNaN = struct.unpack("d", '\x7f\xf0\x00\x00\x00\x00\x00\x01')[0]
# else:
#     raise ValueError, "Byteorder '%s' not supported!" % sys.byteorder
# This one seems better
testNaN = infinity - infinity

# "infinity" for several types
infinityMap = {
    'bool':    [0,          1],
    'int8':    [-2**7,      2**7-1],
    'uint8':   [0,          2**8-1],
    'int16':   [-2**15,     2**15-1],
    'uint16':  [0,          2**16-1],
    'int32':   [-2**31,     2**31-1],
    'uint32':  [0,          2**32-1],
    'int64':   [-2**63,     2**63-1],
    'uint64':  [0,          2**64-1],
    'float32': [-infinityF, infinityF],
    'float64': [-infinity,  infinity], }


# Utility functions
def infType(dtype, itemsize, sign=+1):
    """Return a superior limit for maximum representable data type"""
    assert sign in [-1, +1]

    if dtype.kind == "S":
        if sign < 0:
            return "\x00"*itemsize
        else:
            return "\xff"*itemsize
    try:
        return infinityMap[dtype.name][sign >= 0]
    except KeyError:
        raise TypeError, "Type %s is not supported" % dtype.name


# This check does not work for Python 2.2.x or 2.3.x (!)
def IsNaN(x):
    """a simple check for x is NaN, assumes x is float"""
    return x != x


def PyNextAfter(x, y):
    """returns the next float after x in the direction of y if possible, else returns x"""
    # if x or y is Nan, we don't do much
    if IsNaN(x) or IsNaN(y):
        return x

    # we can't progress if x == y
    if x == y:
        return x

    # similarly if x is infinity
    if x >= infinity or x <= -infinity:
        return x

    # return small numbers for x very close to 0.0
    if -minFloat < x < minFloat:
        if y > x:
            return x + smallEpsilon
        else:
            return x - smallEpsilon  # we know x != y

    # it looks like we have a normalized number
    # break x down into a mantissa and exponent
    m, e = math.frexp(x)

    # all the special cases have been handled
    if y > x:
        m += epsilon
    else:
        m -= epsilon

    return math.ldexp(m, e)


def PyNextAfterF(x, y):
    """returns the next IEEE single after x in the direction of y if possible, else returns x"""

    # if x or y is Nan, we don't do much
    if IsNaN(x) or IsNaN(y):
        return x

    # we can't progress if x == y
    if x == y:
        return x

    # similarly if x is infinity
    if x >= infinityF:
        return infinityF
    elif x <= -infinityF:
        return -infinityF

    # return small numbers for x very close to 0.0
    if -minFloatF < x < minFloatF:
        # since Python uses double internally, we
        # may have some extra precision to toss
        if x > 0.0:
            extra = x % smallEpsilonF
        elif x < 0.0:
            extra = x % -smallEpsilonF
        else:
            extra = 0.0
        if y > x:
            return x - extra + smallEpsilonF
        else:
            return x - extra - smallEpsilonF  # we know x != y

    # it looks like we have a normalized number
    # break x down into a mantissa and exponent
    m, e = math.frexp(x)

    # since Python uses double internally, we
    # may have some extra precision to toss
    if m > 0.0:
        extra = m % epsilonF
    else:  # we have already handled m == 0.0 case
        extra = m % -epsilonF

    # all the special cases have been handled
    if y > x:
        m += epsilonF - extra
    else:
        m -= epsilonF - extra

    return math.ldexp(m, e)


def StringNextAfter(x, direction, itemsize):
    "Return the next representable neighbor of x in the appropriate direction."
    assert direction in [-1, +1]

    # Pad the string with \x00 chars until itemsize completion
    padsize = itemsize - len(x)
    if padsize > 0:
        x += "\x00"*padsize
    xlist = list(x); xlist.reverse()
    i = 0
    if direction > 0:
        if xlist == "\xff"*itemsize:
            # Maximum value, return this
            return "".join(xlist)
        for xchar in xlist:
            if ord(xchar) < 0xff:
                xlist[i] = chr(ord(xchar)+1)
                break
            else:
                xlist[i] = "\x00"
            i += 1
    else:
        if xlist == "\x00"*itemsize:
            # Minimum value, return this
            return "".join(xlist)
        for xchar in xlist:
            if ord(xchar) > 0x00:
                xlist[i] = chr(ord(xchar)-1)
                break
            else:
                xlist[i] = "\xff"
            i += 1
    xlist.reverse()
    return "".join(xlist)


def IntTypeNextAfter(x, direction, itemsize):
    "Return the next representable neighbor of x in the appropriate direction."
    assert direction in [-1, +1]

    # x is guaranteed to be either an int or a float
    if direction < 0:
        if type(x) is int:
            return x-1
        else:
            return int(PyNextAfter(x,x-1))
    else:
        if type(x) is int:
            return x+1
        else:
            return int(PyNextAfter(x,x+1))+1


def nextafter(x, direction, dtype, itemsize):
    "Return the next representable neighbor of x in the appropriate direction."
    assert direction in [-1, 0, +1]
    assert dtype.kind == "S" or type(x) in (int, long, float)

    if direction == 0:
        return x

    if dtype.kind == "S":
        return StringNextAfter(x, direction, itemsize)

    if dtype.kind in ['i', 'u']:
        return IntTypeNextAfter(x, direction, itemsize)
    elif dtype.name == "float32":
        if direction < 0:
            return PyNextAfterF(x,x-1)
        else:
            return PyNextAfterF(x,x+1)
    elif dtype.name == "float64":
        if direction < 0:
            return PyNextAfter(x,x-1)
        else:
            return PyNextAfter(x,x+1)

    raise TypeError("data type ``%s`` is not supported" % dtype)



class IndexProps(object):
    """Container for index properties

    Instance variables:

        auto -- whether an existing index should be updated or not after a
            Table append operation
        reindex -- whether the table fields are to be re-indexed
            after an invalidating index operation (like Table.removeRows)
        filters -- the filter properties for the Table indexes

    """
    auto_default = True
    reindex_default = True

    def __init__( self,
                  auto=auto_default,
                  reindex=reindex_default,
                  filters=None ):
        """Create a new IndexProps instance

        Parameters:

        auto -- whether an existing index should be reindexed after a
            Table append operation. Defaults is reindexing.
        reindex -- whether the table fields are to be re-indexed
            after an invalidating index operation (like Table.removeRows).
            Default is reindexing.
        filters -- the filter properties. Default are ZLIB(1) and shuffle
        """

        self.auto = bool(auto)
        self.reindex = bool(reindex)
        if filters is None:
            self.filters = Filters(complevel=1, complib="zlib",
                                   shuffle=True, fletcher32=False)
        elif isinstance(filters, Filters):
            self.filters = filters
        else:
            raise TypeError, \
"If you pass a filters parameter, it should be a Filters instance."

    def __repr__(self):
        return ( 'IndexProps(auto=%s, reindex=%s, filters=%r)'
                 % (self.auto, self.reindex, self.filters) )

class Index(NotLoggedMixin, indexesExtension.Index, Group):

    """Represent the index (sorted and reverse index) dataset in HDF5 file.

    It enables to create indexes of Columns of Table objects.

    All NumPy datatypes are supported except for complex datatypes.

    Methods:

        search(start, stop, step, where)
        getCoords(startCoords, maxCoords)
        append(object)

    Instance variables:

        column -- The column object this index belongs to
        dirty -- Whether the index is dirty or not.
        nrows -- The number of slices in the index.
        slicesize -- The number of elements per slice.
        nelements -- The number of indexed rows.
        shape -- The shape of this index (in slices and elements).
        filters -- The properties used to filter the stored items.
        sorted -- The IndexArray object with the sorted values information.
        indices -- The IndexArray object with the sorted indices information.

    """

    _c_classId = 'CINDEX'


    # <properties>

    dirty = property(
        lambda self: self.column.dirty, None, None,
        "Whether the index is dirty or not.")

    nblockssuperblock = property(
        lambda self: self.superblocksize / self.blocksize, None, None,
        "The number of blocks in a superblock.")

    nslicesblock = property(
        lambda self: self.blocksize / self.slicesize, None, None,
        "The number of slices in a block.")

    nchunkslice = property(
        lambda self: self.slicesize / self.chunksize, None, None,
        "The number of chunks in a slice.")

    def _g_nsuperblocks(self):
        nblocks = self.nelements / self.superblocksize
        if self.nelements % self.superblocksize > 0:
            nblocks += 1
        return nblocks
    nsuperblocks = property(_g_nsuperblocks , None, None,
        "The total number of superblocks in index.")

    def _g_nblocks(self):
        nblocks = self.nelements / self.blocksize
        if self.nelements % self.blocksize > 0:
            nblocks += 1
        return nblocks
    nblocks = property(_g_nblocks , None, None,
        "The total number of blocks in index.")

    nslices = property(
        lambda self: self.nelements / self.slicesize, None, None,
        "The number of complete slices in index.")

    nchunks = property(
        lambda self: self.nelements / self.chunksize, None, None,
        "The number of complete chunks in index.")

    shape = property(
        lambda self: (self.nrows, self.slicesize), None, None,
        "The shape of this index (in slices and elements).")

    # </properties>


    def __init__(self, parentNode, name,
                 atom=None, column=None,
                 title="", filters=None,
                 optlevel=0,
                 expectedrows=0,
                 testmode=False, new=True):
        """Create an Index instance.

        Keyword arguments:

        atom -- An Atom object representing the shape and type of the
            atomic objects to be saved. Only scalar atoms are
            supported.

        column -- The column object to be indexed

        title -- Sets a TITLE attribute of the Index entity.

        filters -- An instance of the Filters class that provides
            information about the desired I/O filters to be applied
            during the life of this object. If not specified, the ZLIB
            & shuffle will be activated by default (i.e., they are not
            inherited from the parent, that is, the Table).

        optlevel -- The level of optimization for the reordenation indexes.

        expectedrows -- Represents an user estimate about the number
            of row slices that will be added to the growable dimension
            in the IndexArray object.

        """

        self._v_version = None
        """The object version of this index."""

        self._v_expectedrows = expectedrows
        """The expected number of items of index arrays."""
        self.testmode = testmode
        """Enables test mode for index chunk size calculation."""
        if atom is not None:
            self.dtype = atom.dtype.base
            self.type = atom.type
            """The datatypes to be stored by the sorted index array."""
            ############### Important note ###########################
            #The datatypes saved as index values are NumPy native
            #types, so we get rid of type metainfo like Time* or Enum*
            #that belongs to HDF5 types (actually, this metainfo is
            #not needed for sorting and looking-up purposes).
            ##########################################################
        self.column = column
        """The `Column` instance for the indexed column."""

        self.nrows = None
        """The total number of slices in the index."""
        self.nelements = None
        """The number of indexed elements in this index."""
        self.optlevel = optlevel
        """The level of optimization for this index."""
        self.dirtycache = True
        """Dirty cache (for ranges, bounds & sorted) flag."""
        self.tmpfilename = None
        """Filename for temporary bounds."""

        super(Index, self).__init__(
            parentNode, name, title, new, filters)

    def _g_postInitHook(self):
        super(Index, self)._g_postInitHook()

        # Index arrays must only be created for new indexes
        if not self._v_new:
            # Set-up some variables from info on disk and return
            sorted = self.sorted
            self.dtype = sorted.dtype
            self.type = sorted.type
            self.superblocksize = sorted.superblocksize
            self.blocksize = sorted.blocksize
            self.slicesize = sorted.slicesize
            self.chunksize = sorted.chunksize
            self.filters = sorted.filters
            self.reord_opts = sorted.reord_opts
            # The number of elements is at the end of the indices array
            nelementsLR = self.indicesLR[-1]
            self.nrows = sorted.nrows
            self.nelements = self.nrows * self.slicesize + nelementsLR
            self.nelementsLR = nelementsLR
            if nelementsLR > 0:
                self.nrows += 1
            # Get the bounds as a cache (this has to remain here!)
            nboundsLR = (nelementsLR - 1 ) // self.chunksize
            if nboundsLR < 0:
                nboundsLR = 0 # correction for -1 bounds
            nboundsLR += 2 # bounds + begin + end
            # All bounds values (+begin+end) are at the beginning of sortedLR
            self.bebounds = self.sortedLR[:nboundsLR]
            return

        # The index is new. Initialize the values
        self.nrows = 0
        self.nelements = 0

        # Set the filters for this object (they are *not* inherited)
        filters = self._v_new_filters
        if filters is None:
            # If no filters have been passed to the constructor,
            # set a sensible default using zlib compression and shuffling.
            filters = Filters(complevel = 1, complib = "zlib",
                              shuffle = 1, fletcher32 = 0)
        self.filters = filters

        # Create the IndexArray for sorted values
        atom = Atom.from_dtype(self.dtype)
        sorted = IndexArray(self, 'sorted', atom, (0,),
                            "Sorted Values", filters, self.optlevel,
                            self.testmode, self._v_expectedrows)

        # After "sorted" is created, we can assign some attributes
        self.superblocksize = sorted.superblocksize
        self.blocksize = sorted.blocksize
        self.slicesize = sorted.slicesize
        self.chunksize = sorted.chunksize
        self.reord_opts = sorted.reord_opts

        # Create the IndexArray for index values
        IndexArray(self, 'indices',
                   Int64Atom(), (0,), "Reverse Indices",
                   filters, self.optlevel,
                   self.testmode, self._v_expectedrows)

        # Create the cache for range values  (1st order cache)
        CacheArray(self, 'ranges', atom, (0,2), "Range Values", filters,
                   self._v_expectedrows//self.slicesize)
        # median ranges
        EArray(self, 'mranges', atom, (0,), "Median ranges", filters,
               _log=False)

        # Create the cache for boundary values (2nd order cache)
        nbounds_inslice = (self.slicesize - 1 ) // self.chunksize
        CacheArray(self, 'bounds', atom, (0, nbounds_inslice),
                   "Boundary Values", filters,
                   self._v_expectedrows//self.chunksize)

        # begin, end & median bounds (only for numeric types)
        EArray(self, 'abounds', atom, (0,), "Start bounds", _log=False)
        EArray(self, 'zbounds', atom, (0,), "End bounds", filters, _log=False)
        EArray(self, 'mbounds', atom, (0,), "Median bounds", filters,
               _log=False)

        # Create the Array for last (sorted) row values + bounds
        shape = (2 + nbounds_inslice + self.slicesize,)
        arr = numpy.empty(shape=shape, dtype=self.dtype)
        sortedLR = LastRowArray(self, 'sortedLR', arr,
                                "Last Row sorted values + bounds")

        # Create the Array for reverse indexes in last row
        shape = (self.slicesize,)     # enough for indexes and length
        arr = numpy.zeros(shape=shape, dtype='int64')
        LastRowArray(self, 'indicesLR', arr,
                     "Last Row reverse indices")

        # All bounds values (+begin+end) are at the beginning of sortedLR
        nboundsLR = 0   # 0 bounds initially
        self.bebounds = sortedLR[:nboundsLR]

        # The starts and lengths initialization
        self.starts = numpy.empty(shape=self.nrows, dtype=numpy.int32)
        """Where the values fulfiling conditions starts for every slice."""
        self.lengths = numpy.empty(shape=self.nrows, dtype=numpy.int32)
        """Lengths of the values fulfilling conditions for every slice."""


    def _g_updateDependent(self):
        super(Index, self)._g_updateDependent()
        self.column._updateIndexLocation(self)


    def append(self, arr):
        """Append the array to the index objects"""

        # Objects that arrive here should be numpy objects already
        # Save the sorted array
        sorted = self.sorted
        s=arr.argsort()
        # Doing a sort in-place is 2x slower than a fancy selection
        #arr.sort()
        arr = arr[s]
        # Indexes in PyTables Pro systems are 64-bit long.
        offset = sorted.nrows * self.slicesize
        s = numpy.array(s, dtype="int64") + offset
        self.indices.append(s)
        sorted.append(arr)
        cs = self.chunksize
        ncs = self.nchunkslice
        self.ranges.append([arr[[0,-1]]])
        self.bounds.append([arr[cs::cs]])
        self.abounds.append(arr[0::cs])
        self.zbounds.append(arr[cs-1::cs])
        # Compute the medians
        smedian = arr[cs/2::cs]
        self.mbounds.append(smedian)
        self.mranges.append([smedian[ncs/2]])
        # Update nrows after a successful append
        self.nrows = sorted.nrows
        self.nelements = self.nrows * self.slicesize
        self.nelementsLR = 0  # reset the counter of the last row index to 0
        self.dirtycache = True   # the cache is dirty now


    def appendLastRow(self, arr, tnrows):
        """Append the array to the last row index objects"""

        # compute the elements in the last row sorted & bounds array
        sorted = self.sorted
        indicesLR = self.indicesLR
        sortedLR = self.sortedLR
        offset = sorted.nrows * self.slicesize
        nelementsLR = tnrows - offset
        assert nelementsLR == len(arr), \
"The number of elements to append is incorrect!. Report this to the authors."
        # Sort the array
        s = arr.argsort()
        arr = arr[s]
        # build the cache of bounds
        self.bebounds = numpy.concatenate((arr[::self.chunksize],
                                           [arr[-1]]))
        # Save the reverse index array
        s = numpy.array(s, dtype="int64") + offset
        indicesLR[:len(arr)] = s
        # The number of elements is at the end of the array
        indicesLR[-1] = nelementsLR
        # Save the number of elements, bounds and sorted values
        offset = len(self.bebounds)
        sortedLR[:offset] = self.bebounds
        sortedLR[offset:offset+len(arr)] = arr
        # Update nelements after a successful append
        self.nrows = sorted.nrows + 1
        self.nelements = sorted.nrows * self.slicesize + nelementsLR
        self.nelementsLR = nelementsLR
        self.dirtycache = True   # the cache is dirty now


    def optimize(self, level=None, verbose=False):
        "Optimize an index to allow faster searches."

        self.verbose=verbose
        #self.verbose = True  # uncomment for debugging purposes only

        # Initialize last_tover
        self.last_tover = 0

        # Optimize only when we have more than one slice
        if self.nslices <= 1:
            if verbose:
                print "Less than 1 slice. Skipping optimization!"
            return

        if self.verbose:
            (nover, mult, tover) = self.compute_overlaps("init", self.verbose)

        if level is not None:
            optmedian, optstarts, optstops, optfull = (False,)*4
            if 3 <= level < 6:
                optstarts = True
            elif 6 <= level < 9:
                optstarts = True
                optstops = True
            elif level == 9:
                optfull = True
        else:
            optmedian, optstarts, optstops, optfull = self.reord_opts

        # Start the optimization process
        if optmedian or optstarts or optstops or optfull:
            create_tmp = True
            swap_done = True
        else:
            create_tmp = False
            swap_done = False
        while True:
            if create_tmp:
                if self.swap('create'):
                    swap_done = False  # No swap has been done!
                    break
            if optfull:
                if self.swap('chunks', 'median'): break
                if self.nblocks > 1:
                    # Swap slices only in the case that we have several blocks
                    if self.swap('slices', 'median'): break
                    if self.swap('chunks','median'): break
                if self.swap('chunks', 'start'): break
                if self.swap('chunks', 'stop'): break
            else:
                if optmedian:
                    if self.swap('chunks', 'median'): break
                if optstarts:
                    if self.swap('chunks', 'start'): break
                if optstops:
                    if self.swap('chunks', 'stop'): break
            break  # If we reach this, exit the loop
        # Close and delete the temporal optimization index file
        if create_tmp:
            self.cleanup_temps()
            if swap_done:
                # the memory data cache is dirty now
                self.dirtycache = True
        return


    def swap(self, what, mode=None):
        "Swap chunks or slices using a certain bounds reference."

        # Thresholds for avoiding continuing the optimization
        thnover = 4        # minimum number of overlapping slices
        thmult = 0.01      # minimum ratio of multiplicity (a 1%)
        thtover = 0.001    # minimum overlaping index for slices (a .1%)
        if self.verbose:
            t1 = time();  c1 = clock()
        if what == "create":
            self.create_temps()
        elif what == "chunks":
            self.swap_chunks(mode)
        elif what == "slices":
            self.swap_slices(mode)
        if mode:
            message = "swap_%s(%s)" % (what, mode)
        else:
            message = "swap_%s" % (what,)
        (nover, mult, tover) = self.compute_overlaps(message, self.verbose)
        rmult = len(mult.nonzero()[0]) / float(len(mult))
        if self.verbose:
            t = round(time()-t1, 4);  c = round(clock()-c1, 4)
            print "time: %s. clock: %s" % (t, c)
        # Check that entropy is actually decreasing
        if what == "chunks" and self.last_tover > 0. and tover > 0.:
            tover_var = (self.last_tover - tover) / self.last_tover
            if tover_var < 0.1:
                # Less than a 10% of improvement is too few
                return True
        self.last_tover = tover
        # Check if some threshold has met
        if nover < thnover:
            return True
        if rmult < thmult:
            return True
        # Additional check for the overlap ratio
        if tover >= 0. and tover < thtover:
            return True
        return False


    def create_temps(self):
        "Create some temporary objects for slice sorting purposes."

        # The algorithms for doing the swap can be optimized so that
        # one should be necessary to create temporaries for keeping just
        # the contents of a single superblock.
        # F. Altet 2007-01-03
        # Build the name of the temporary file
        dirname = os.path.dirname(self._v_file.filename)
        fd, self.tmpfilename = tempfile.mkstemp(".idx", "pytables-", dirname)
        # Close the file descriptor so as to avoid leaks
        os.close(fd)
        # Create the proper PyTables file
        self.tmpfile = openFile(self.tmpfilename, "w")
        self.tmp = self.tmpfile.root
        cs = self.chunksize
        ss = self.slicesize
        #filters = self.filters
        # compressing temporaries is very inefficient!
        filters = None
        # temporary sorted & indices arrays
        shape = (self.nrows, ss)
        atom = Atom.from_dtype(self.dtype)
        CArray(self.tmp, 'sorted', atom, shape,
               "Temporary sorted", filters, chunkshape=(1,cs))
        CArray(self.tmp, 'indices', Int64Atom(), shape,
               "Temporary indices", filters, chunkshape=(1,cs))
        # temporary bounds
        nbounds_inslice = (ss - 1) // cs
        shape = (self.nslices, nbounds_inslice)
        CArray(self.tmp, 'bounds', atom, shape, "Temp chunk bounds",
               filters, chunkshape=(cs, nbounds_inslice))
        shape = (self.nchunks,)
        CArray(self.tmp, 'abounds', atom, shape, "Temp start bounds",
               filters, chunkshape=(cs,))
        CArray(self.tmp, 'zbounds', atom, shape, "Temp end bounds",
               filters, chunkshape=(cs,))
        CArray(self.tmp, 'mbounds', atom, shape, "Median bounds",
               filters, chunkshape=(cs,))
        # temporary ranges
        CArray(self.tmp, 'ranges', atom, (self.nslices, 2),
               "Temporary range values", filters, chunkshape=(cs,2))
        CArray(self.tmp, 'mranges', atom, (self.nslices,),
               "Median ranges", filters, chunkshape=(cs,))


    def cleanup_temps(self):
        "Delete the temporaries for sorting purposes."
        if self.verbose:
            print "Deleting temporaries..."
        self.tmp = None
        self.tmpfile.close()
        os.remove(self.tmpfilename)
        self.tmpfilename = None


    def swap_chunks(self, mode="median"):
        "Swap & reorder the different chunks in a block."

        boundsnames = {'start':'abounds', 'stop':'zbounds', 'median':'mbounds'}
        sorted = self.sorted
        indices = self.indices
        tmp_sorted = self.tmp.sorted
        tmp_indices = self.tmp.indices
        tsorted = numpy.empty(shape=self.slicesize, dtype=self.dtype)
        tindices = numpy.empty(shape=self.slicesize, dtype='int64')
        cs = self.chunksize
        ncs = self.nchunkslice
        nsb = self.nslicesblock
        ncb = ncs * nsb
        ncb2 = ncb
        boundsobj = self._v_file.getNode(self, boundsnames[mode])
        for nblock in xrange(self.nblocks):
            # Protection for last block having less chunks than ncb
            remainingchunks = self.nchunks - nblock*ncb
            if remainingchunks < ncb:
                # To avoid reordering the chunks in last row (slice)
                # This last row reordering should be supported later on
                ncb2 = (remainingchunks/ncs)*ncs
            if ncb2 <= 1:
                # if only zero or one chunks remains we are done
                break
            nslices = ncb2/ncs
            bounds = boundsobj[nblock*ncb:nblock*ncb+ncb2]
            sbounds_idx = numpy.argsort(bounds)
            # Don't swap the block at all if it doesn't need to
            ndiff = (sbounds_idx != numpy.arange(ncb2)).sum()/2
            if ndiff*20 < ncb2:
                # The number of chunks to rearrange is less than 5%,
                # so skip the reordering of this superblock
                # (too expensive for such a little improvement)
                if self.verbose:
                    print "skipping reordering of block-->", nblock, ndiff, ncb2
                continue
            # Swap sorted and indices following the new order
            offset = nblock*nsb
            for i in xrange(nslices):
                ns = offset + i;
                # Get sorted & indices slices in new order
                for j in xrange(ncs):
                    idx = sbounds_idx[i*ncs+j]
                    ins = idx / ncs;  inc = (idx - ins*ncs)*cs
                    ins += offset
                    nc = j * cs
                    tsorted[nc:nc+cs] = sorted[ins,inc:inc+cs]
                    tindices[nc:nc+cs] = indices[ins,inc:inc+cs]
                tmp_sorted[ns] = tsorted
                tmp_indices[ns] = tindices
            # Reorder completely indices at slice level
            self.reorder_slices(mode, nblock)


    def reorder_slices(self, mode, nblock):
        "Reorder completely a block at slice level."

        sorted = self.sorted
        indices = self.indices
        tmp_sorted = self.tmp.sorted
        tmp_indices = self.tmp.indices
        cs = self.chunksize
        ncs = self.nchunkslice
        nsb = self.nslicesblock
        # First, reorder the complete slices
        for nslice in xrange(nblock*nsb, (nblock+1)*nsb):
            # Protection against processing non-existing slices
            if nslice >= self.sorted.nrows:
                break
            block = tmp_sorted[nslice]
            sblock_idx = numpy.argsort(block)
            block = block[sblock_idx]
            sorted[nslice] = block
            block_idx = tmp_indices[nslice]
            indices[nslice] = block_idx[sblock_idx]
            self.ranges[nslice] = block[[0,-1]]
            self.bounds[nslice] = block[cs::cs]
            # update start & stop bounds
            self.abounds[nslice*ncs:(nslice+1)*ncs] = block[0::cs]
            self.zbounds[nslice*ncs:(nslice+1)*ncs] = block[cs-1::cs]
            # update median bounds
            smedian = block[cs/2::cs]
            self.mbounds[nslice*ncs:(nslice+1)*ncs] = smedian
            self.mranges[nslice] = smedian[ncs/2]


    def swap_slices(self, mode="median"):
        "Swap slices in a superblock."

        sorted = self.sorted
        indices = self.indices
        tmp_sorted = self.tmp.sorted
        tmp_indices = self.tmp.indices
        ncs = self.nchunkslice
        nss = self.superblocksize / self.slicesize
        nss2 = nss
        for sblock in xrange(self.nsuperblocks):
            # Protection for last superblock having less slices than nss
            remainingslices = self.nslices - sblock*nss
            if remainingslices < nss:
                nss2 = remainingslices
            if nss2 <= 1:
                break
            if mode == "start":
                ranges = self.ranges[sblock*nss:sblock*nss+nss2, 0]
            elif mode == "stop":
                ranges = self.ranges[sblock*nss:sblock*nss+nss2, 1]
            elif mode == "median":
                ranges = self.mranges[sblock*nss:sblock*nss+nss2]
            sranges_idx = numpy.argsort(ranges)
            # Don't swap the superblock at all if it doesn't need to
            ndiff = (sranges_idx != numpy.arange(nss2)).sum()/2
            if ndiff*50 < nss2:
                # The number of slices to rearrange is less than 2.5%,
                # so skip the reordering of this superblock
                # (too expensive for such a little improvement)
                if self.verbose:
                    print "skipping reordering of superblock-->", sblock
                continue
            ns = sblock*nss2
            # Swap sorted and indices slices following the new order
            for i in xrange(nss2):
                idx = sranges_idx[i]
                # Swap sorted & indices slices
                oi = ns+i; oidx = ns+idx
                tmp_sorted[oi] = sorted[oidx]
                tmp_indices[oi] = indices[oidx]
                # Swap start, stop & median ranges
                self.tmp.ranges[oi] = self.ranges[oidx]
                self.tmp.mranges[oi] = self.mranges[oidx]
                # Swap chunk bounds
                self.tmp.bounds[oi] = self.bounds[oidx]
                # Swap start, stop & median bounds
                j = oi*ncs; jn = (oi+1)*ncs
                xj = oidx*ncs; xjn = (oidx+1)*ncs
                self.tmp.abounds[j:jn] = self.abounds[xj:xjn]
                self.tmp.zbounds[j:jn] = self.zbounds[xj:xjn]
                self.tmp.mbounds[j:jn] = self.mbounds[xj:xjn]
            # tmp --> originals
            for i in xrange(nss2):
                # Copy sorted & indices slices
                oi = ns+i
                sorted[oi] = tmp_sorted[oi]
                indices[oi] = tmp_indices[oi]
                # Copy start, stop & median ranges
                self.ranges[oi] = self.tmp.ranges[oi]
                self.mranges[oi] = self.tmp.mranges[oi]
                # Copy chunk bounds
                self.bounds[oi] = self.tmp.bounds[oi]
                # Copy start, stop & median bounds
                j = oi*ncs; jn = (oi+1)*ncs
                self.abounds[j:jn] = self.tmp.abounds[j:jn]
                self.zbounds[j:jn] = self.tmp.zbounds[j:jn]
                self.mbounds[j:jn] = self.tmp.mbounds[j:jn]


    def compute_overlaps(self, message, verbose):
        """Compute some statistics about overlaping of slices in index.

        It returns the following info:

        noverlaps -- The total number of slices that overlaps in index (int).
        multiplicity -- The number of times that a concrete slice overlaps
            with any other (array of ints).
        toverlap -- An ovelap index: the sum of the values in segment slices
            that overlaps divided by the entire range of values (float).
            This index is only computed for numerical types.
        """

        ranges = self.ranges[:]
        nslices = self.nslices
        noverlaps = 0; soverlap = 0.; toverlap = -1.
        multiplicity = numpy.zeros(shape=nslices, dtype="int_")
        for i in xrange(nslices):
            for j in xrange(i+1, nslices):
                if ranges[i,1] > ranges[j,0]:
                    noverlaps += 1
                    multiplicity[j-i] += 1
                    if self.type != "string":
                        # Convert ranges into floats in order to allow
                        # doing operations with them without overflows
                        soverlap += float(ranges[i,1]) - float(ranges[j,0])
        # Return the overlap as the ratio between overlaps and entire range
        if self.type != "string":
            erange = float(ranges[-1,1]) - float(ranges[0,0])
            # Check that there is an effective range of values
            # Beware, erange can be negative in situations where
            # the values are suffering overflow. This can happen
            # specially on big signed integer values (on overflows,
            # the end value will become negative!).
            # Also, there is no way to compute overlap ratios for
            # non-numerical types. So, be careful and always check
            # that toverlap has a positive value (it must be
            # initialized to -1. above) before using it.
            # F. Altet 2007-01-19
            if erange > 0:
                toverlap = soverlap / erange
        if verbose:
            print "overlaps (%s):" % message, noverlaps, toverlap
            print multiplicity
        return (noverlaps, multiplicity, toverlap)


    def restorecache(self):
        "Clean the limits cache and resize starts and lengths arrays"

        self.limboundscache = ObjectCache(LIMBOUNDS_MAX_SLOTS,
                                          LIMBOUNDS_MAX_SIZE,
                                          'bounds limits')
        self.starts = numpy.empty(shape=self.nrows, dtype = numpy.int32)
        self.lengths = numpy.empty(shape=self.nrows, dtype = numpy.int32)
        # Initialize the sorted array in extension
        self.sorted._initSortedSlice(self)
        self.dirtycache = False


    # This is an optimized version of search.
    # It does not work well with strings, because:
    # In [180]: a=strings.array(None, itemsize = 4, shape=1)
    # In [181]: a[0] = '0'
    # In [182]: a >= '0\x00\x00\x00\x01'
    # Out[182]: array([1], type=Bool)  # Incorrect
    # but...
    # In [183]: a[0] >= '0\x00\x00\x00\x01'
    # Out[183]: False  # correct
    #
    # While this is not a bug (see the padding policy for chararrays)
    # I think it would be much better to use '\0x00' as default padding
    #
    def search(self, item):
        """Do a binary search in this index for an item"""

        if self.dirtycache:
            self.restorecache()

        # An empty item means that the number of records is always
        # going to be empty, so we avoid further computation
        # (including looking up the limits cache).
        if not item:
            self.starts[:] = 0
            self.lengths[:] = 0
            return 0

        tlen = 0
        # Check whether the item tuple is in the limits cache or not
        nslot = self.limboundscache.getslot(item)
        if nslot >= 0:
            startlengths = self.limboundscache.getitem(nslot)
            # Reset the lengths array (the starts is not necessary)
            self.lengths[:] = 0
            # Now, set the interesting rows
            for nrow in xrange(len(startlengths)):
                nrow2, start, length = startlengths[nrow]
                self.starts[nrow2] = start
                self.lengths[nrow2] = length
                tlen = tlen + length
            return tlen
        # The item is not in cache. Do the real lookup.
        sorted = self.sorted
        if sorted.nrows > 0:
            if self.type != "string":
                item1, item2 = item
                # The next are optimizations. However, they hide the
                # CPU functions consumptions from python profiles.
                # Activate only after development is done.
                if self.type == "float64":
                    tlen = sorted._searchBinNA_d(item1, item2)
                elif self.type == "int32":
                    tlen = sorted._searchBinNA_i(item1, item2)
                elif self.type == "int64":
                    tlen = sorted._searchBinNA_ll(item1, item2)
                else:
                    tlen = self.search_scalar(item, sorted)
            else:
                tlen = self.search_scalar(item, sorted)
        # Get possible remaing values in last row
        if self.nelementsLR > 0:
            # Look for more indexes in the last row
            (start, stop) = self.searchLastRow(item)
            self.starts[-1] = start
            self.lengths[-1] = stop - start
            tlen += stop - start

        if self.limboundscache.couldenablecache():
            # Get a startlengths tuple and save it in cache.
            # This is quite slow, but it is a good way to compress
            # the bounds info. Moreover, the .couldenablecache()
            # is doing a good work so as to avoid computing this
            # when it is not necessary to do it.
            startlengths = []
            for nrow, length in enumerate(self.lengths):
                if length > 0:
                    startlengths.append((nrow, self.starts[nrow], length))
            # Compute the size of the recarray (aproximately)
            # The +1 at the end is important to avoid 0 lengths
            # (remember, the object headers take some space)
            size = len(startlengths) * 8 * 2 + 1
            # Put this startlengths list in cache
            self.limboundscache.setitem(item, startlengths, size)

        return tlen


    # This is an scalar version of search. It works well with strings as well.
    def search_scalar(self, item, sorted):
        """Do a binary search in this index for an item."""
        tlen = 0
        # Do the lookup for values fullfilling the conditions
        for i in xrange(sorted.nrows):
            (start, stop) = sorted._searchBin(i, item)
            self.starts[i] = start
            self.lengths[i] = stop - start
            tlen += stop - start
        return tlen


    def searchLastRow(self, item):
        item1, item2 = item
        item1done = 0; item2done = 0

        #t1=time()
        hi = hi2 = self.nelementsLR               # maximum number of elements
        bebounds = self.bebounds
        assert hi == self.nelements - self.sorted.nrows * self.slicesize
        begin = bebounds[0]
        # Look for items at the beginning of sorted slices
        if item1 <= begin:
            result1 = 0
            item1done = 1
        if item2 < begin:
            result2 = 0
            item2done = 1
        if item1done and item2done:
            return (result1, result2)
        # Then, look for items at the end of the sorted slice
        end = bebounds[-1]
        if not item1done:
            if item1 > end:
                result1 = hi
                item1done = 1
        if not item2done:
            if item2 >= end:
                result2 = hi
                item2done = 1
        if item1done and item2done:
            return (result1, result2)
        # Finally, do a lookup for item1 and item2 if they were not found
        # Lookup in the middle of the slice for item1
        bounds = bebounds[1:-1] # Get the bounds array w/out begin and end
        nbounds = len(bebounds)
        readSliceLR = self.sortedLR._readSortedSlice
        nchunk = -1
        if not item1done:
            # Search the appropriate chunk in bounds cache
            nchunk = bisect.bisect_left(bounds, item1)
            end = self.chunksize*(nchunk+1)
            if end > hi:
                end = hi
            chunk = readSliceLR(self.sorted, nbounds+self.chunksize*nchunk,
                                nbounds+end)
            if len(chunk) < hi:
                hi2 = len(chunk)
            result1 = bisect.bisect_left(chunk, item1, 0, hi2)
            result1 += self.chunksize*nchunk
        # Lookup in the middle of the slice for item2
        if not item2done:
            # Search the appropriate chunk in bounds cache
            nchunk2 = bisect.bisect_right(bounds, item2)
            if nchunk2 <> nchunk:
                end = self.chunksize*(nchunk2+1)
                if end > hi:
                    end = hi
                chunk = readSliceLR(self.sorted, nbounds+self.chunksize*nchunk2,
                                    nbounds+end)
                if len(chunk) < hi:
                    hi2 = len(chunk)
            result2 = bisect.bisect_right(chunk, item2, 0, hi2)
            result2 += self.chunksize*nchunk2
        #t = time()-t1
        #print "time searching indices (last row):", round(t*1000, 3), "ms"
        return (result1, result2)


    def getLookupRange(self, ops, limits, table):
        assert len(ops) in [1, 2]
        assert len(limits) in [1, 2]
        assert len(ops) == len(limits)

        column = self.column
        coldtype = column.dtype.base
        itemsize = coldtype.itemsize

        if len(limits) == 1:
            assert ops[0] in ['lt', 'le', 'eq', 'ge', 'gt']
            limit = limits[0]
            op = ops[0]
            if op == 'lt':
                range_ = (infType(coldtype, itemsize, sign=-1),
                          nextafter(limit, -1, coldtype, itemsize))
            elif op == 'le':
                range_ = (infType(coldtype, itemsize, sign=-1),
                          limit)
            elif op == 'gt':
                range_ = (nextafter(limit, +1, coldtype, itemsize),
                          infType(coldtype, itemsize, sign=+1))
            elif op == 'ge':
                range_ = (limit,
                          infType(coldtype, itemsize, sign=+1))
            elif op == 'eq':
                range_ = (limit, limit)

        elif len(limits) == 2:
            assert ops[0] in ['gt', 'ge'] and ops[1] in ['lt', 'le']

            lower, upper = limits
            if lower > upper:
                # ``a <[=] x <[=] b`` is always false if ``a > b``.
                return ()

            if ops == ['gt', 'lt']:  # lower < col < upper
                range_ = (nextafter(lower, +1, coldtype, itemsize),
                          nextafter(upper, -1, coldtype, itemsize))
            elif ops == ['ge', 'lt']:  # lower <= col < upper
                range_ = (lower, nextafter(upper, -1, coldtype, itemsize))
            elif ops == ['gt', 'le']:  # lower < col <= upper
                range_ = (nextafter(lower, +1, coldtype, itemsize), upper)
            elif ops == ['ge', 'le']:  # lower <= col <= upper
                range_ = (lower, upper)

        return range_


    def _f_remove(self, recursive=False):
        """Remove this Index object"""

        # Index removal is always recursive,
        # no matter what `recursive` says.
        super(Index, self)._f_remove(True)


    def __str__(self):
        """This provides a more compact representation than __repr__"""
        return "Index(%s, shape=%s, chunksize=%s)" % \
               (self.nelements, self.shape, self.sorted.chunksize)


    def __repr__(self):
        """This provides more metainfo than standard __repr__"""

        cpathname = self.column.table._v_pathname + ".cols." + self.column.name
        retstr = """%s (Index for column %s)
  type := %r
  nelements := %s
  shape := %s
  chunksize := %s
  byteorder := %r
  filters := %s
  dirty := %s
  sorted := %s
  indices := %s""" % (self._v_pathname, cpathname,
                     self.sorted.type, self.nelements, self.shape,
                     self.sorted.chunksize, self.sorted.byteorder,
                     self.filters, self.dirty, self.sorted, self.indices)
        retstr += "\n  ranges := %s" % self.ranges
        retstr += "\n  bounds := %s" % self.bounds
        retstr += "\n  sortedLR := %s" % self.sortedLR
        retstr += "\n  indicesLR := %s" % self.indicesLR
        return retstr