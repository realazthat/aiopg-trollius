try:
    # Use builtin asyncio on Python 3.4+, or Tulip on Python 3.3
    import asyncio
    from asyncio import From,Return
except ImportError:
    # Use Trollius on Python <= 3.2
    import trollius as asyncio
    from trollius import From,Return

import collections
import sys
import warnings


from psycopg2.extensions import TRANSACTION_STATUS_IDLE

from .connection import connect, TIMEOUT
from .log import logger


PY_341 = sys.version_info >= (3, 4, 1)


@asyncio.coroutine
def create_pool(dsn=None, forcenamedarguments=None, minsize=10, maxsize=10,
                loop=None, timeout=TIMEOUT,
                enable_json=True, enable_hstore=True,
                echo=False,
                **kwargs):
    if loop is None:
        loop = asyncio.get_event_loop()

    pool = Pool(dsn, minsize, maxsize, loop, timeout,
                enable_json=enable_json, enable_hstore=enable_hstore,
                echo=echo,
                **kwargs)
    if minsize > 0:
        with (yield From( pool._cond )):
            yield From( pool._fill_free_pool(False) )
    raise Return( pool )


class Pool(asyncio.AbstractServer):
    """Connection pool"""

    def __init__(self, dsn, minsize, maxsize, loop, timeout, forcenamedarguments=None,
                 enable_json=True, enable_hstore=True,
                 echo=False, **kwargs):
    
        if minsize < 0:
            raise ValueError("minsize should be zero or greater")
        if maxsize < minsize:
            raise ValueError("maxsize should be not less than minsize")
        self._dsn = dsn
        self._minsize = minsize
        self._loop = loop
        self._timeout = timeout
        self._enable_json = enable_json
        self._enable_hstore = enable_hstore
        self._echo = echo
        self._conn_kwargs = kwargs
        self._acquiring = 0
        self._free = collections.deque(maxlen=maxsize)
        self._cond = asyncio.Condition(loop=loop)
        self._used = set()
        self._terminated = set()
        self._closing = False
        self._closed = False

    @property
    def echo(self):
        return self._echo

    @property
    def minsize(self):
        return self._minsize

    @property
    def maxsize(self):
        return self._free.maxlen

    @property
    def size(self):
        return self.freesize + len(self._used) + self._acquiring

    @property
    def freesize(self):
        return len(self._free)

    @property
    def timeout(self):
        return self._timeout

    @asyncio.coroutine
    def clear(self):
        """Close all free connections in pool."""
        with (yield From( self._cond )):
            while self._free:
                conn = self._free.popleft()
                yield From( conn.close() )
            self._cond.notify()

    def close(self):
        """Close pool.

        Mark all pool connections to be closed on getting back to pool.
        Closed pool doesn't allow to acquire new connections.
        """
        if self._closed:
            return
        self._closing = True

    def terminate(self):
        """Terminate pool.

        Close pool with instantly closing all acquired connections also.
        """

        self.close()

        for conn in list(self._used):
            conn.close()
            self._terminated.add(conn)

        self._used.clear()

    @asyncio.coroutine
    def wait_closed(self):
        """Wait for closing all pool's connections."""

        if self._closed:
            raise Return()
        if not self._closing:
            raise RuntimeError(".wait_closed() should be called "
                               "after .close()")

        while self._free:
            conn = self._free.popleft()
            conn.close()

        with (yield From(self._cond)):
            while self.size > self.freesize:
                yield From( self._cond.wait() )

        self._closed = True

    @asyncio.coroutine
    def acquire(self):
        """Acquire free connection from the pool."""
        if self._closing:
            raise RuntimeError("Cannot acquire connection after closing pool")
        with (yield From( self._cond )):
            while True:
                yield From( self._fill_free_pool(True) )
                if self._free:
                    conn = self._free.popleft()
                    assert not conn.closed, conn
                    assert conn not in self._used, (conn, self._used)
                    self._used.add(conn)
                    raise Return( conn )
                else:
                    yield From( self._cond.wait() )

    @asyncio.coroutine
    def _fill_free_pool(self, override_min):
        while self.size < self.minsize:
            self._acquiring += 1
            try:
                conn = yield From( connect(
                    self._dsn, loop=self._loop, timeout=self._timeout,
                    enable_json=self._enable_json,
                    enable_hstore=self._enable_hstore,
                    echo=self._echo,
                    **self._conn_kwargs) )
                # raise exception if pool is closing
                self._free.append(conn)
                self._cond.notify()
            finally:
                self._acquiring -= 1
        if self._free:
            raise Return()

        if override_min and self.size < self.maxsize:
            self._acquiring += 1
            try:
                conn = yield From( connect(
                    self._dsn, loop=self._loop, timeout=self._timeout,
                    enable_json=self._enable_json,
                    enable_hstore=self._enable_hstore,
                    echo=self._echo,
                    **self._conn_kwargs))
                # raise exception if pool is closing
                self._free.append(conn)
                self._cond.notify()
            finally:
                self._acquiring -= 1

    @asyncio.coroutine
    def _wakeup(self):
        with (yield From( self._cond )):
            self._cond.notify()

    def release(self, conn):
        """Release free connection back to the connection pool.

        This is NOT a coroutine.
        """
        if conn in self._terminated:
            assert conn.closed, conn
            self._terminated.remove(conn)
            return
        assert conn in self._used, (conn, self._used)
        self._used.remove(conn)
        if not conn.closed:
            tran_status = conn._conn.get_transaction_status()
            if tran_status != TRANSACTION_STATUS_IDLE:
                logger.warning(
                    "Invalid transaction status on released connection: %d",
                    tran_status)
                conn.close()
                return
            if self._closing:
                conn.close()
            else:
                self._free.append(conn)
            asyncio.Task(self._wakeup(), loop=self._loop)

    @asyncio.coroutine
    def cursor(self, name=None, cursor_factory=None,
               scrollable=None, withhold=False, forcenamedarguments=None, timeout=None):
        """XXX"""
        conn = yield From( self.acquire() )
        cur = yield From( conn.cursor(name=name, cursor_factory=cursor_factory,
                                     scrollable=scrollable, withhold=withhold,
                                     timeout=timeout) )
        raise Return( _CursorContextManager(self, conn, cur) )

    def __enter__(self):
        raise RuntimeError(
            '"yield from" should be used as context manager expression')

    def __exit__(self, *args):
        # This must exist because __enter__ exists, even though that
        # always raises; that's how the with-statement works.
        pass  # pragma: nocover

    def __iter__(self):
        # This is not a coroutine.  It is meant to enable the idiom:
        #
        #     with (yield from pool) as conn:
        #         <block>
        #
        # as an alternative to:
        #
        #     conn = yield from pool.acquire()
        #     try:
        #         <block>
        #     finally:
        #         conn.release()
        conn = yield From( self.acquire() )
        raise Return( _ConnectionContextManager(self, conn) )

    if PY_341:  # pragma: no branch
        def __del__(self):
            try:
                self._free
            except AttributeError:
                return  # frame has been cleared, __dict__ is empty
            if self._free:
                left = 0
                while self._free:
                    conn = self._free.popleft()
                    conn.close()
                    left += 1
                warnings.warn(
                    "Unclosed {} connections in {!r}".format(left, self),
                    ResourceWarning)


class _ConnectionContextManager:
    """Context manager.

    This enables the following idiom for acquiring and releasing a
    connection around a block:

        with (yield from pool) as conn:
            cur = yield from conn.cursor()

    while failing loudly when accidentally using:

        with pool:
            <block>
    """

    __slots__ = ('_pool', '_conn')

    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *args):
        try:
            self._pool.release(self._conn)
        finally:
            self._pool = None
            self._conn = None


class _CursorContextManager:
    """Context manager.

    This enables the following idiom for acquiring and releasing a
    cursor around a block:

        with (yield from pool.cursor()) as cur:
            yield from cur.execute("SELECT 1")

    while failing loudly when accidentally using:

        with pool:
            <block>
    """

    __slots__ = ('_pool', '_conn', '_cur')

    def __init__(self, pool, conn, cur):
        self._pool = pool
        self._conn = conn
        self._cur = cur

    def __enter__(self):
        return self._cur

    def __exit__(self, *args):
        try:
            self._cur._impl.close()
            self._pool.release(self._conn)
        finally:
            self._pool = None
            self._conn = None
            self._cur = None
