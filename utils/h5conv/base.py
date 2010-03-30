import os, numpy, h5py
from scipy.sparse import csc_matrix

VERSION = '0.3'
VERSION_MLDATA = '0'
NUM_EXTRACT = 10
COMPRESSION = None


class H5Converter(object):
    """Base converter class.

    @cvar str_type: string type to be used for variable length strings in h5py
    @type str_type: numpy.dtype

    @ivar fname_in: filename to read data from
    @type fname_in: string
    @ivar fname_out: filename to write converted data to
    @type fname_out: string
    @ivar labels_idx: indices for labels for each row
    @type labels_idx: list of integers
    """

    str_type = h5py.new_vlen(numpy.str)


    def __init__(self, fname_in, fname_out):
        self.fname_in = fname_in
        self.fname_out = fname_out
        self.labels_idx = None


    def warn(self, msg):
        """Print a warning message.

        @param msg: message to print
        @type msg: string
        """
        return
        print 'WARNING: ' + msg


    def get_name(self):
        """Get dataset name from given file.

        @return: comment
        @rtype: string
        """
        # without str() it might barf
        return str(os.path.basename(self.fname_in).split('.')[0])


    def get_comment(self):
        """Get comment from given file.

        @return: comment
        @rtype: string
        """
        raise NotImplementedError('Abstract method!')


    def get_types(self):
        """Get attribute/data types, if available."""
        return None


    def get_data(self):
        """Get data from given file.

        @return: data names, ordering and examples
        @rtype: dict of: list of names, list of ordering and dict of examples
        """
        raise NotImplementedError('Abstract method!')


    def _get_merged(self, data):
        """Merge given data where appropriate.

        String arrays are not merged, but all int and all double are merged
        into one matrix.

        @param data: data structure as returned by get_data()
        @type data: dict
        @return: merged data structure
        @rtype: dict
        """
        # nothing to do if we have one sparse matrix
        if 'data' and 'indices' and 'indptr' in data['ordering']:
            return data

        merged = {}
        for name, val in data['data'].iteritems():
            if len(val) < 1:
                continue

            t = type(val[0])
            if t == numpy.int32:
                path = 'int'
            elif t == numpy.double:
                path = 'double'
            else:
                if name.find('/') != -1: # / sep belongs to hdf5 path
                    path = name.replace('/', '+')
                    data['ordering'][data['ordering'].index(name)] = path
                else:
                    path = name
                merged[path] = val
                continue

            if not path in merged:
                merged[path] = []
            merged[path].append(val)

        data['data'] = merged
        return data


    def run(self):
        """Run the actual conversion process."""
        h5file = h5py.File(self.fname_out, 'w')

        h5file.attrs['name'] = self.get_name()
        h5file.attrs['mldata'] = VERSION_MLDATA
        h5file.attrs['comment'] = self.get_comment()

        data = self._get_merged(self.get_data())
        group = h5file.create_group('/data')
        for path, val in data['data'].iteritems():
            group.create_dataset(path, data=val, compression=COMPRESSION)

        group = h5file.create_group('/data_descr')
        names = numpy.array(data['names']).astype(self.str_type)
        group.create_dataset('names', data=names, compression=COMPRESSION)
        ordering = numpy.array(data['ordering']).astype(self.str_type)
        group.create_dataset('ordering', data=ordering, compression=COMPRESSION)
        types = self.get_types()
        if types:
            types = numpy.array(types).astype(self.str_type)
            group.create_dataset('types', data=types, compression=COMPRESSION)

        h5file.close()