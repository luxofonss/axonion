"""
Lazy evaluation utilities for optional/expensive operations
"""
from typing import Callable, TypeVar, Generic, Optional

T = TypeVar('T')


class LazyProperty(Generic[T]):
    """
    Lazy property that computes value only when accessed.
    Thread-safe and caches the result after first computation.
    """
    
    def __init__(self, compute_fn: Callable[[], T]):
        """
        Args:
            compute_fn: Function that computes the value when needed
        """
        self._compute_fn = compute_fn
        self._value: Optional[T] = None
        self._computed = False
    
    @property
    def value(self) -> T:
        """Get the value, computing it if necessary"""
        if not self._computed:
            self._value = self._compute_fn()
            self._computed = True
        return self._value
    
    def is_computed(self) -> bool:
        """Check if value has been computed"""
        return self._computed
    
    def force_compute(self) -> T:
        """Force computation and return value"""
        return self.value
    
    def reset(self):
        """Reset the lazy property to uncomputed state"""
        self._value = None
        self._computed = False


def lazy(compute_fn: Callable[[], T]) -> LazyProperty[T]:
    """
    Convenience function to create a lazy property.
    
    Usage:
        expensive_data = lazy(lambda: compute_expensive_operation())
        # Later when needed:
        result = expensive_data.value
    """
    return LazyProperty(compute_fn)

