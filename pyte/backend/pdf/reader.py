
import re, struct, time

from binascii import unhexlify
from collections import OrderedDict
from io import BytesIO, SEEK_CUR, SEEK_END
from math import floor

from . import cos, filter
from .util import FIFOBuffer
from ...util import all_subclasses


DICTIONARY_SUBCLASSES = {}
for cls in all_subclasses(cos.Dictionary):
    if cls.type is not None:
        DICTIONARY_SUBCLASSES.setdefault((cls.type, cls.subtype), cls)

FILTER_SUBCLASSES = {cls.__name__: cls
                     for cls in all_subclasses(filter.Filter)}



class PDFReader(cos.Document):
    def __init__(self, file_or_filename):
        try:
            self.file = open(file_or_filename, 'rb')
        except TypeError:
            self.file = file_or_filename
        self.timestamp = time.time()
        xref_offset = self.find_xref_offset()
        self._xref = self.parse_xref_table(xref_offset)
        self._by_object_id = {}
        trailer = self.parse_trailer()
        if 'Info' in trailer:
            self.info = trailer['Info']
        else:
            self.info = cos.Dictionary()
        self.id = trailer['ID'] if 'ID' in trailer else None
        self._max_identifier_in_file = int(trailer['Size']) - 1
        self.catalog = trailer['Root']

    @property
    def max_identifier(self):
        return max(super().max_identifier, self._max_identifier_in_file)

    def __getitem__(self, identifier):
        try:
            obj = super().__getitem__(identifier)
        except KeyError:
            address = self._xref[identifier]
            obj_identifier, obj = self.parse_indirect_object(address)
            assert obj_identifier == identifier
            self[identifier] = obj
        return obj

    def __delitem__(self, identifier):
        del self._xref[identifier]
        super().__delitem__(identifier)

    def jump_to_next_line(self):
        while True:
            char = self.file.read(1)
            if char == b'\n':
                break
            elif char == b'\r':
                next_char = self.file.read(1)
                if next_char != b'\n':
                    self.file.seek(-1, SEEK_CUR)
                break

    def eat_whitespace(self):
        while True:
            char = self.file.read(1)
            if char not in cos.WHITESPACE:
                self.file.seek(-1, SEEK_CUR)
                break

    def next_token(self):
        token = self.file.read(1)
        if token in (cos.HexString.PREFIX, cos.HexString.POSTFIX):
            # check for dict begin/end
            char = self.file.read(1)
            if char == token:
                token += char
            else:
                self.file.seek(-1, SEEK_CUR)
        elif token in cos.DELIMITERS + cos.WHITESPACE:
            pass
        else:
            while True:
                char = self.file.read(1)
                if char in cos.DELIMITERS + cos.WHITESPACE:
                    self.file.seek(-1, SEEK_CUR)
                    break
                token += char
        return token

    def next_item(self, indirect=False):
        self.eat_whitespace()
        restore_pos = self.file.tell()
        token = self.next_token()
        if token == cos.String.PREFIX:
            item = self.read_string(indirect)
        elif token == cos.HexString.PREFIX:
            item = self.read_hex_string(indirect)
        elif token == cos.Array.PREFIX:
            item = self.read_array(indirect)
        elif token == cos.Name.PREFIX:
            item = self.read_name(indirect)
        elif token == cos.Dictionary.PREFIX:
            item = self.read_dictionary_or_stream(indirect)
        elif token == b'true':
            item = cos.Boolean(True, indirect=indirect)
        elif token == b'false':
            item = cos.Boolean(False, indirect=indirect)
        elif token == b'null':
            item = cos.Null(indirect=indirect)
        else:
            # number or indirect reference
            self.file.seek(restore_pos)
            item = self.read_number(indirect)
            restore_pos = self.file.tell()
            if isinstance(item, cos.Integer):
                try:
                    generation = self.read_number()
                    self.eat_whitespace()
                    r = self.next_token()
                    if isinstance(generation, cos.Integer) and r == b'R':
                        item = cos.Reference(self, int(item), int(generation))
                    else:
                        raise ValueError
                except ValueError:
                    self.file.seek(restore_pos)
        return item

    def peek(self, length=50):
        restore_pos = self.file.tell()
        print(self.file.read(length))
        self.file.seek(restore_pos)

    def read_array(self, indirect=False):
        array = cos.Array(indirect=indirect)
        while True:
            self.eat_whitespace()
            token = self.file.read(1)
            if token == cos.Array.POSTFIX:
                break
            self.file.seek(-1, SEEK_CUR)
            item = self.next_item()
            array.append(item)
        return array

    def read_name(self, indirect=False):
        name = b''
        while True:
            char = self.file.read(1)
            if char in cos.DELIMITERS + cos.WHITESPACE:
                self.file.seek(-1, SEEK_CUR)
                break
            elif char == b'#':
                char_code = self.file.read(2)
                char = chr(int(char_code.decode('ascii'), 16)).encode('ascii')
            name += char
        return cos.Name(name, indirect=indirect)

    def read_dictionary_or_stream(self, indirect=False):
        dictionary = cos.Dictionary(indirect=indirect)
        while True:
            self.eat_whitespace()
            token = self.next_token()
            if token == cos.Dictionary.POSTFIX:
                break
            key, value = self.read_name(), self.next_item()
            dictionary[key] = value
        self.eat_whitespace()
        dict_pos = self.file.tell()
        if self.next_token() == b'stream':
            self.jump_to_next_line()
            length = int(dictionary['Length'])
            stream = cos.Stream()
            stream.update(dictionary)
            stream._data.write(self.file.read(length))
            self.eat_whitespace()
            assert self.next_token() == b'endstream'
            dictionary = stream
            if 'Filter' in dictionary:
                stream_filter = str(dictionary['Filter'])
                dictionary._filter = FILTER_SUBCLASSES[stream_filter]()
        else:
            self.file.seek(dict_pos)
        # try to map to specific Dictionary sub-class
        type = dictionary.get('Type', None)
        subtype = dictionary.get('Subtype', None)
        key = str(type) if type else None, str(subtype) if subtype else None
        if key in DICTIONARY_SUBCLASSES:
            dictionary.__class__ = DICTIONARY_SUBCLASSES[key]
        return dictionary

    escape_chars = b'nrtbf()\\'

    def read_string(self, indirect=False):
        string = b''
        escape = False
        parenthesis_level = 0   # TODO: is currently not used
        while True:
            char = self.file.read(1)
            if escape:
                if char in self.escape_chars:
                    string += char
                elif char == b'\n':
                    pass
                elif char == b'\r' and self.file.read(1) != '\n':
                    self.file.seek(-1, SEEK_CUR)
                elif char.isdigit():
                    for i in range(2):
                        extra = self.file.read(1)
                        if extra.isdigit():
                            char += extra
                        else:
                            self.file.seek(-1, SEEK_CUR)
                            break
                    string += struct.pack('B', int(char, 8))
                else:
                    string += b'\\' + char
                escape = False
            elif char == b'\\':
                escape = True
            elif char == b'(':
                parenthesis_level += 1
            elif char == b')' and parenthesis_level > 0:
                parenthesis_level -= 1
            elif char == cos.String.POSTFIX:
                break
            else:
                string += char
        return cos.String(string, indirect=indirect)

    def read_hex_string(self, indirect=False):
        hex_string = b''
        while True:
            self.eat_whitespace()
            char = self.file.read(1)
            if char == cos.HexString.POSTFIX:
                break
            hex_string += char
        if len(hex_string) % 2 > 0:
            hex_string += b'0'
        return cos.HexString(unhexlify(hex_string), indirect=indirect)

    def read_number(self, indirect=False):
        self.eat_whitespace()
        number_string = b''
        while True:
            char = self.file.read(1)
            if char not in b'+-.0123456789':
                self.file.seek(-1, SEEK_CUR)
                break
            number_string += char
        try:
            number = cos.Integer(number_string, indirect=indirect)
        except ValueError:
            number = cos.Real(number_string, indirect=indirect)
        return number

    def parse_trailer(self):
        assert self.next_token() == b'trailer'
        self.jump_to_next_line()
        trailer_dict = self.next_item()
        if 'XRefStm' in trailer_dict:
            xref_stm = self.parse_xref_stream(trailer_dict['XRefStm'])
            xref_stm.update(self._xref)
            self._xref = xref_stm
        elif 'Prev' in trailer_dict:
            prev_xref = self.parse_xref_table(trailer_dict['Prev'])
            prev_xref.update(self._xref)
            self._xref = prev_xref
            self.parse_trailer()
        return trailer_dict
##/Size: (Required; must not be an indirect reference) The total number of entries in the file's
##cross-reference table, as defined by the combination of the original section and all
##update sections. Equivalently, this value is 1 greater than the highest object number
##used in the file.
##Note: Any object in a cross-reference section whose number is greater than this value is
##ignored and considered missing.

    def parse_indirect_object(self, address):
        # save file state
        restore_pos = self.file.tell()
        self.file.seek(address)
        identifier = int(self.read_number())
        generation = int(self.read_number())
        self.eat_whitespace()
        assert self.next_token() == b'obj'
        self.eat_whitespace()
        obj = self.next_item(indirect=True)
        reference = cos.Reference(self, identifier, generation)
        self._by_object_id[id(obj)] = reference
        self.eat_whitespace()
        assert self.next_token() == b'endobj'
        self.file.seek(restore_pos)
        return identifier, obj

    def parse_xref_table(self, offset):
        xref = {}
        self.file.seek(offset)
        assert self.next_token() == b'xref'
        while True:
            try:
                first, total = int(self.read_number()), self.read_number()
                self.jump_to_next_line()
                for identifier in range(first, first + total):
                    line = self.file.read(20)
                    if line[17] == ord(b'n'):
                        address, generation = int(line[:10]), int(line[11:16])
                        xref[identifier] = address
                    else:
                        assert line[17] == ord(b'f')
                        assert int(line[11:16]) == 65535
            except ValueError:
                break
        return xref

    def parse_xref_stream(self, offset):
        identifier, xref_stream = self.parse_indirect_object(offset)
        self[identifier] = xref_stream
        xref = {}
        widths = [int(width) for width in xref_stream['W']]
        index = iter(int(value) for value in xref_stream['Index'])

        columns = int(xref_stream['DecodeParms']['Columns'])
        xref_stream.seek(0)
        reconstructor = PNGReconstructor(xref_stream, columns)
        while True:
            try:
                first, total = next(index), next(index)
            except StopIteration:
                break
            for identifier in range(first, first + total):
                print(identifier, end='  ')
                for width in widths:
                    byte = struct.unpack('>B', reconstructor.read(width))[0]
                    print(byte, end=' ')
                print()
        assert identifier + 1 == int(xref_stream['Size'])
        import sys; sys.exit()

    def find_xref_offset(self):
        self.file.seek(0, SEEK_END)
        offset = self.file.tell() - len('%%EOF')
        while True:
            self.file.seek(offset)
            value = self.file.read(len('startxref'))
            if value == b'startxref':
                self.jump_to_next_line()
                xref_offset = self.read_number()
                self.jump_to_next_line()
                if self.file.read(5) != b'%%EOF':
                    raise ValueError('Invalid PDF file: missing %%EOF')
                break
            offset -= 1
        return int(xref_offset)


PREDICTOR_STRUCT = struct.Struct('>B')


NONE = 0
SUB = 1
UP = 2
AVERAGE = 3
PAETH = 4


class PNGReconstructor(FIFOBuffer):
    # TODO: bitsper...
    def __init__(self, source, columns):
        super().__init__(source)
        self.columns = columns
        self._column_struct = struct.Struct('>{}B'.format(columns))
        self._last_values = [0] * columns

    def read_from_source(self, n):
        # number of bytes requested `n` is ignored; a single row is fetched
        predictor = PREDICTOR_STRUCT.unpack(self._source.read(1))[0]
        row = self._source.read(self._column_struct.size)
        values = list(self._column_struct.unpack(row))

        if predictor == NONE:
            out_row = row
        elif predictor == SUB:
            recon_a = 0
            for index, filt_x in enumerate(values):
                recon_a = values[index] = (filt_x + recon_a) % 256
            out_row = self._column_struct.pack(*values)
        elif predictor == UP:
            for index, (filt_x, recon_b) in enumerate(zip(values,
                                                          self._last_values)):
                values[index] = (filt_x + recon_b) % 256
            out_row = self._column_struct.pack(*values)
        elif predictor == AVERAGE:
            recon_a = 0
            for index, (filt_x, recon_b) in enumerate(zip(values,
                                                          self._last_values)):
                average = (recon_a + recon_b) // 2
                recon_a = values[index] = (filt_x + average) % 256
            out_row = self._column_struct.pack(*values)
        elif predictor == PAETH:
            recon_a = recon_c = 0
            for index, (filt_x, recon_b) in enumerate(zip(values,
                                                          self._last_values)):
                prediction = paeth_predictor(recon_a, recon_b, recon_c)
                recon_a = values[index] = (filt_x + prediction) % 256
            out_row = self._column_struct.pack(*values)

        self._last_values = values
        return out_row


def paeth_predictor(a, b, c):
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    elif pb <= pc:
        return b
    else:
        return c
