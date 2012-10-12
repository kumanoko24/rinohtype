
from .tables import OpenTypeTable, MultiFormatTable, context_array, offset_array
from .parse import fixed, int16, uint16, tag, glyph_id, offset, array, indirect
from .parse import Packed
from .layout import LayoutTable, ScriptListTable, FeatureListTable, LookupTable
from .layout import Coverage, ClassDefinition


class ValueFormat(Packed):
    reader = uint16
    fields = [('XPlacement', 0x0001, bool),
              ('YPlacement', 0x0002, bool),
              ('XAdvance', 0x0004, bool),
              ('YAdvance', 0x0008, bool),
              ('XPlaDevice', 0x0010, bool),
              ('YPlaDevice', 0x0020, bool),
              ('XAdvDevice', 0x0040, bool),
              ('YAdvDevice', 0x0080, bool)]


class SingleAdjustmentSubtable(OpenTypeTable):
    pass


class Class2Record(OpenTypeTable):
    def __init__(self, file, format_1, format_2):
        super().__init__(file)
        self['Value1'] = ValueRecord(file, format_1)
        self['Value2'] = ValueRecord(file, format_2)


# TODO: MultiFormatTable
class PairAdjustmentSubtable(MultiFormatTable):
    entries = [('PosFormat', uint16),
               ('Coverage', indirect(Coverage)),
               ('ValueFormat1', ValueFormat),
               ('ValueFormat2', ValueFormat)]
    formats = {1: [('PairSetCount', uint16)],
               2: [('ClassDef1', indirect(ClassDefinition)),
                   ('ClassDef2', indirect(ClassDefinition)),
                   ('Class1Count', uint16),
                   ('Class2Count', uint16)]}

    def __init__(self, file, file_offset):
        super().__init__(file, file_offset)
        format_1, format_2 = self['ValueFormat1'], self['ValueFormat2']
        if self['PosFormat'] == 1:
            pst_reader = (lambda file, file_offset: PairSetTable(file,
                                                                 file_offset,
                                                                 format_1,
                                                                 format_2))
            self['PairSet'] = (offset_array(pst_reader, 'PairSetCount')
                                   (self, file, file_offset))
        elif self['PosFormat'] == 2:
            self['Class1Record'] = [[Class2Record(file, format_1, format_2)
                                     for j in range(self['Class2Count'])]
                                    for i in range(self['Class1Count'])]

    def lookup(self, a_id, b_id):
        if self['PosFormat'] == 1:
            try:
                index = self['Coverage'].index(a_id)
            except ValueError:
                raise KeyError
            pair_value_record = self['PairSet'][index].by_second_glyph_id[b_id]
            return pair_value_record['Value1']['XAdvance']
        elif self['PosFormat'] == 2:
            a_class = self['ClassDef1'].class_number(a_id)
            b_class = self['ClassDef2'].class_number(b_id)
            class_2_record = self['Class1Record'][a_class][b_class]
            return class_2_record['Value1']['XAdvance']


class PairSetTable(OpenTypeTable):
    entries = [('PairValueCount', uint16)]

    def __init__(self, file, file_offset, format_1, format_2):
        super().__init__(file, file_offset)
        pvr_reader = lambda file: PairValueRecord(file, format_1, format_2)
        self['PairValueRecord'] = array(pvr_reader, self['PairValueCount'])(file)
        self.by_second_glyph_id = {}
        for record in self['PairValueRecord']:
            self.by_second_glyph_id[record['SecondGlyph']] = record


class PairValueRecord(Class2Record):
    entries = [('SecondGlyph', glyph_id)]


class ValueRecord(OpenTypeTable):
    formats = {'XPlacement': int16,
               'YPlacement': int16,
               'XAdvance': int16,
               'YAdvance': int16,
               'XPlaDevice': offset,
               'YPlaDevice': offset,
               'XAdvDevice': offset,
               'YAdvDevice': offset}

    def __init__(self, file, value_format):
        super().__init__(file, None)
        for name, present in value_format.items():
            if present:
                self[name] = self.formats[name](file)


class GposTable(LayoutTable):
    """Glyph positioning table"""
    tag = 'GPOS'
    lookup_types = {1: SingleAdjustmentSubtable,
                    2: PairAdjustmentSubtable}