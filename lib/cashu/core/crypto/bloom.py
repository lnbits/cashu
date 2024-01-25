import hashlib
from typing import Generic, TypeVar

T = TypeVar("T")


class BloomFilter(Generic[T]):
    def __init__(self, size: int, hash_func=hashlib.sha256):
        self._size = size
        self._hash_func = hash_func
        self._bit_array = [False] * size

    def add(self, item: T) -> None:
        for i in range(len(item)):
            digest = self._hash_func(str(item[i]).encode()).digest()
            position = int.from_bytes(digest[:4], byteorder="big") % self._size
            self._bit_array[position] = True

    def lookup(self, item: T) -> bool:
        for i in range(len(item)):
            digest = self._hash_func(str(item[i]).encode()).digest()
            position = int.from_bytes(digest[:4], byteorder="big") % self._size
            if not self._bit_array[position]:
                return False
        return True


bloom = BloomFilter(100)
bloom.add("hello".encode())
bloom.add("how".encode())
bloom.add("are".encode())
bloom.add("you".encode())


print(bloom.lookup("mee"))
print(bloom.lookup("asd"))
print(bloom.lookup("are"))
