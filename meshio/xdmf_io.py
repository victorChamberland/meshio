# -*- coding: utf-8 -*-
#
'''
I/O for XDMF.

.. moduleauthor:: Nico Schlömer <nico.schloemer@gmail.com>
'''
import os
try:
    from StringIO import cStringIO as BytesIO
except ImportError:
    from io import BytesIO
import xml.etree.ElementTree as ET

import h5py
import numpy

from .vtk_io import cell_data_from_raw, raw_from_cell_data
from .vtu_io import write_xml


def read(filename):
    return XdmfReader(filename).read()


def _xdmf_to_numpy_type(data_type, precision):
    if data_type == 'Int' and precision == '4':
        return numpy.int32
    elif data_type == 'Int' and precision == '8':
        return numpy.int64
    elif data_type == 'Float' and precision == '4':
        return numpy.float32

    assert data_type == 'Float' and precision == '8', \
        'Unknown XDMF type ({}, {}).'.format(data_type, precision)
    return numpy.float64


numpy_to_xdmf_dtype = {
    numpy.dtype(numpy.int32): ('Int', '4'),
    numpy.dtype(numpy.int64): ('Int', '8'),
    numpy.dtype(numpy.float32): ('Float', '4'),
    numpy.dtype(numpy.float64): ('Float', '8'),
    }

xdmf_idx_to_meshio_type = {
    1: 'vertex',
    4: 'triangle',
    5: 'quad',
    6: 'tetra',
    7: 'pyramid',
    8: 'wedge',
    9: 'hexahedron',
    }
meshio_type_to_xdmf_index = {v: k for k, v in xdmf_idx_to_meshio_type.items()}

xdmf_to_meshio_type = {
    'Polyvertex': 'vertex',
    'Triangle': 'triangle',
    'Quadrilateral': 'quad',
    'Tetrahedron': 'tetra',
    'Pyramid': 'pyramid',
    'Wedge': 'wedge',
    'Hexahedron': 'hexahedron',
    'Edge_3': 'line3',
    'Tri_6': 'triangle6',
    'Quad_8': 'quad8',
    'Tet_10': 'tetra10',
    'Pyramid_13': 'pyramid13',
    'Wedge_15': 'wedge15',
    'Hex_20': 'hexahedron20',
    }
meshio_to_xdmf_type = {v: k for k, v in xdmf_to_meshio_type.items()}


def _translate_mixed_cells(data):
    # Translate it into the cells dictionary.
    # `data` is a one-dimensional vector with
    # (cell_type1, p0, p1, ... ,pk, cell_type2, p10, p11, ..., p1k, ...

    # http://www.xdmf.org/index.php/XDMF_Model_and_Format#Topology
    # https://gitlab.kitware.com/xdmf/xdmf/blob/master/XdmfTopologyType.hpp#L394
    xdmf_idx_to_num_nodes = {
        1: 1,  # vertex
        4: 3,  # triangle
        5: 4,  # quad
        6: 4,  # tet
        7: 5,  # pyramid
        8: 6,  # wedge
        9: 8,  # hex
        11: 6,  # triangle6
        }

    # collect types and offsets
    types = []
    offsets = []
    r = 0
    while r < len(data):
        types.append(data[r])
        offsets.append(r)
        r += xdmf_idx_to_num_nodes[data[r]] + 1

    offsets = numpy.array(offsets)

    # Collect types into bins.
    # See <https://stackoverflow.com/q/47310359/353337> for better
    # alternatives.
    uniques = numpy.unique(types)
    bins = {u: numpy.where(types == u)[0] for u in uniques}

    cells = {}
    for tpe, b in bins.items():
        meshio_type = xdmf_idx_to_meshio_type[tpe]
        assert (data[offsets[b]] == tpe).all()
        n = xdmf_idx_to_num_nodes[tpe]
        indices = numpy.array([
            numpy.arange(1, n+1) + o for o in offsets[b]
            ])
        cells[meshio_type] = data[indices]

    return cells


class XdmfReader(object):
    def __init__(self, filename):
        self.filename = filename
        return

    def read(self):
        tree = ET.parse(self.filename)
        root = tree.getroot()

        assert root.tag == 'Xdmf'

        version = root.attrib['Version']

        if version.split('.')[0] == '2':
            return self.read_xdmf2(root)

        assert version.split('.')[0] == '3', \
            'Unknown XDMF version {}.'.format(version)

        return self.read_xdmf3(root)

    def read_data_item(self, data_item, dt_key='DataType'):
        dims = [int(d) for d in data_item.attrib['Dimensions'].split()]
        data_type = data_item.attrib[dt_key]
        precision = data_item.attrib['Precision']

        if data_item.attrib['Format'] == 'XML':
            return numpy.array(
                data_item.text.split(),
                dtype=_xdmf_to_numpy_type(data_type, precision)
                ).reshape(dims)

        assert data_item.attrib['Format'] == 'HDF', \
            'Unknown XDMF Format \'{}\'.'.format(
                    data_item.attrib['Format']
                    )

        info = data_item.text.strip()
        filename, h5path = info.split(':')

        # The HDF5 file path is given with respect to the XDMF (XML) file.
        full_hdf5_path = os.path.join(
                os.path.dirname(self.filename),
                filename
                )

        f = h5py.File(full_hdf5_path, 'r')
        assert h5path[0] == '/'

        for key in h5path[1:].split('/'):
            f = f[key]
        # `[()]` gives a numpy.ndarray
        return f[()]

    def read_xdmf2(self, root):
        domains = list(root)
        assert len(domains) == 1
        domain = domains[0]
        assert domain.tag == 'Domain'

        grids = list(domain)
        assert len(grids) == 1, \
            'XDMF reader: Only supports one grid right now.'
        grid = grids[0]
        assert grid.tag == 'Grid'
        assert grid.attrib['GridType'] == 'Uniform'

        points = None
        cells = {}
        point_data = {}
        cell_data_raw = {}
        field_data = {}

        for c in grid:
            if c.tag == 'Topology':
                data_items = list(c)
                assert len(data_items) == 1
                meshio_type = xdmf_to_meshio_type[c.attrib['TopologyType']]
                cells[meshio_type] = self.read_data_item(
                    data_items[0], dt_key='NumberType'
                    )

            elif c.tag == 'Geometry':
                assert c.attrib['GeometryType'] == 'XYZ'
                data_items = list(c)
                assert len(data_items) == 1
                points = self.read_data_item(
                        data_items[0], dt_key='NumberType'
                        )

            else:
                assert c.tag == 'Attribute', \
                    'Unknown section \'{}\'.'.format(c.tag)

                # assert c.attrib['Active'] == '1'
                # assert c.attrib['AttributeType'] == 'None'

                data_items = list(c)
                assert len(data_items) == 1

                data = self.read_data_item(data_items[0], dt_key='NumberType')

                name = c.attrib['Name']
                if c.attrib['Center'] == 'Node':
                    point_data[name] = data
                elif c.attrib['Center'] == 'Cell':
                    cell_data_raw[name] = data
                else:
                    # TODO
                    assert c.attrib['Center'] == 'Grid'

        cell_data = cell_data_from_raw(cells, cell_data_raw)

        return points, cells, point_data, cell_data, field_data

    def read_xdmf3(self, root):
        domains = list(root)
        assert len(domains) == 1
        domain = domains[0]
        assert domain.tag == 'Domain'

        grids = list(domain)
        assert len(grids) == 1, \
            'XDMF reader: Only supports one grid right now.'
        grid = grids[0]
        assert grid.tag == 'Grid'

        points = None
        cells = {}
        point_data = {}
        cell_data_raw = {}
        field_data = {}

        for c in grid:
            if c.tag == 'Topology':
                data_items = list(c)
                assert len(data_items) == 1
                data_item = data_items[0]

                data = self.read_data_item(data_item)

                if c.attrib['Type'] == 'Mixed':
                    cells = _translate_mixed_cells(data)
                else:
                    meshio_type = xdmf_to_meshio_type[c.attrib['Type']]
                    cells[meshio_type] = data

            elif c.tag == 'Geometry':
                assert c.attrib['Type'] == 'XYZ'
                data_items = list(c)
                assert len(data_items) == 1
                data_item = data_items[0]
                points = self.read_data_item(data_item)

            else:
                assert c.tag == 'Attribute', \
                    'Unknown section \'{}\'.'.format(c.tag)

                assert c.attrib['Type'] == 'None'

                data_items = list(c)
                assert len(data_items) == 1
                data_item = data_items[0]

                data = self.read_data_item(data_item)

                name = c.attrib['Name']
                if c.attrib['Center'] == 'Node':
                    point_data[name] = data
                else:
                    assert c.attrib['Center'] == 'Cell'
                    cell_data_raw[name] = data

        cell_data = cell_data_from_raw(cells, cell_data_raw)

        return points, cells, point_data, cell_data, field_data


def write(filename,
          points,
          cells,
          point_data=None,
          cell_data=None,
          field_data=None,
          pretty_xml=True
          ):
    # from .legacy_writer import write as w
    # w('xdmf3', filename, points, cells, point_data, cell_data, field_data)
    # exit(1)

    def numpy_to_xml_string(data, fmt):
        s = BytesIO()
        numpy.savetxt(s, data.flatten(), fmt)
        return s.getvalue().decode()

    xdmf_file = ET.Element(
        'Xdmf',
        Version='3.0',
        )

    domain = ET.SubElement(xdmf_file, 'Domain')
    grid = ET.SubElement(domain, 'Grid', Name='Grid')

    # points
    geo = ET.SubElement(grid, 'Geometry', Origin='', Type='XYZ')
    dt, prec = numpy_to_xdmf_dtype[points.dtype]
    dim = '{} {}'.format(*points.shape)
    data_item = ET.SubElement(
            geo, 'DataItem',
            DataType=dt, Dimensions=dim, Format='XML', Precision=prec
            )
    data_item.text = numpy_to_xml_string(points, '%.15e')

    # cells
    if len(cells) == 1:
        meshio_type = list(cells.keys())[0]
        xdmf_type = meshio_to_xdmf_type[meshio_type]
        topo = ET.SubElement(grid, 'Topology', Type=xdmf_type)
        dt, prec = numpy_to_xdmf_dtype[cells[meshio_type].dtype]
        dim = '{} {}'.format(*cells[meshio_type].shape)
        data_item = ET.SubElement(
                topo, 'DataItem',
                DataType=dt, Dimensions=dim, Format='XML', Precision=prec
                )
        data_item.text = numpy_to_xml_string(cells[meshio_type], '%d')
    elif len(cells) > 1:
        topo = ET.SubElement(grid, 'Topology', Type='Mixed')
        total_num_cells = sum(c.shape[0] for c in cells.values())
        total_num_cell_items = sum(numpy.prod(c.shape) for c in cells.values())
        dim = str(total_num_cell_items + total_num_cells)
        # Deliberately take the data type of the first key
        keys = list(cells.keys())
        dt, prec = numpy_to_xdmf_dtype[cells[keys[0]].dtype]
        data_item = ET.SubElement(
                topo, 'DataItem',
                DataType=dt, Dimensions=dim, Format='XML', Precision=prec
                )
        # prepend column with index
        data_item.text = ''
        for key, value in cells.items():
            d = numpy.column_stack([
                numpy.full(len(value), meshio_type_to_xdmf_index[key]),
                value
                ])
            data_item.text += numpy_to_xml_string(d, '%d')

    # point data
    for name, data in point_data.items():
        att = ET.SubElement(
                grid, 'Attribute',
                Name=name, Type='None', Center='Node'
                )
        dt, prec = numpy_to_xdmf_dtype[data.dtype]
        dim = ' '.join([str(s) for s in data.shape])
        data_item = ET.SubElement(
                att, 'DataItem',
                DataType=dt, Dimensions=dim, Format='XML', Precision=prec
                )
        data_item.text = numpy_to_xml_string(data, '%.15e')

    # cell data
    raw = raw_from_cell_data(cell_data)
    for name, data in raw.items():
        att = ET.SubElement(
                grid, 'Attribute',
                Name=name, Type='None', Center='Cell'
                )
        dt, prec = numpy_to_xdmf_dtype[data.dtype]
        dim = ' '.join([str(s) for s in data.shape])
        data_item = ET.SubElement(
                att, 'DataItem',
                DataType=dt, Dimensions=dim, Format='XML', Precision=prec
                )
        data_item.text = numpy_to_xml_string(data, '%.15e')

    ET.register_namespace('xi', 'http://www.w3.org/2001/XInclude')

    write_xml(filename, xdmf_file, pretty_xml, indent=2)
    return