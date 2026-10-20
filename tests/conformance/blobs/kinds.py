"""The value type; importing it registers nothing."""


class Blob:
    def __init__(self, data: bytes):
        self.data = data

    def __eq__(self, other):
        return isinstance(other, Blob) and other.data == self.data

    def __hash__(self):
        return hash(self.data)
