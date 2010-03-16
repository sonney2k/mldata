import numpy, h5py, os
from scipy.sparse import csc_matrix
import base



class LIBSVM2H5(base.H5Converter):
    """Convert a file from LibSVM to HDF5."""

    def __init__(self, *args, **kwargs):
        """Constructor.

        @ivar offset_labels: indices for labels for each row
        @type offset_labels: list of integers
        """
        super(LIBSVM2H5, self).__init__(*args, **kwargs)
        self.offset_labels = []


    def _explode_labels(self, label):
        """Explode labels to be prepended to data row.

        This is needed for multilabel support.

        @param label: labels read from data file
        @type label: list of characters
        @return: exploded labels
        @rtype: list of integers
        """
        label = numpy.double(''.join(label).split(','))
        ll = []
        if len(label) > 1:
            for l in label:
                ll.append([l, 1])
            self.offset_labels.append(int(max(label)))
        else:
            ll.append([0, label[0]])
            self.offset_labels.append(0)
        return ll


    def _parse_line(self, line):
        """Parse a LibSVM input line and return attributes.

        @param line: line to parse
        @type line: string
        @return: attributes in this line
        @rtype: list of attributes
        """
        state = 'label'
        idx = []
        val = []
        label = []
        attributes = []
        for c in line:
            if state == 'label':
                if c.isspace():
                    state = 'idx'
                    attributes.extend(self._explode_labels(label))
                else:
                    label.append(c)
            elif state == 'idx':
                if not c.isspace():
                    if c == ':':
                        state = 'preval'
                    else:
                        idx.append(c)
            elif state == 'preval':
                if not c.isspace():
                    val.append(c)
                    state = 'val'
            elif state == 'val':
                if c.isspace():
                    attributes.append([int(''.join(idx)) + self.offset_labels[-1], ''.join(val)])
                    idx = []
                    val = []
                    state = 'idx'
                else:
                    val.append(c)

        return attributes


    def get_matrix(self):
        """Retrieves a SciPy Compressed Sparse Column matrix from file.

        @return: compressed sparse column matrix
        @rtype: scipy.sparse.csc_matrix
        """
        self.offset_labels = []
        indices = []
        indptr = [0]
        data = []
        ptr = 0
        infile = open(self.fname_in, 'r')

        for line in infile:
            attributes = self._parse_line(line)
            for a in attributes:
                indices.append(int(a[0]))
                data.append(numpy.double(a[1]))
                ptr += 1
            indptr.append(ptr)
        infile.close()

        return csc_matrix((numpy.array(data), numpy.array(indices), numpy.array(indptr)))


    def get_comment(self):
        return 'LibSVM'


    def get_data(self):
        A = self.get_matrix()
        data = {}
        if A.nnz/numpy.double(A.shape[0]*A.shape[1]) < 0.5: # sparse
            data['indices'] = A.indices
            data['indptr'] = A.indptr
            data['data'] = A.data
            order = ['data_indices', 'data_indptr', 'data_data']
        else: # dense
            data['data'] = A.todense().T
            order = ['data']

        names = []
        for i in xrange(A.shape[0]):
            names.append('dim' + str(i))

        return {'order':order, 'names':names, 'data':data}
