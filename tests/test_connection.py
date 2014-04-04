import asyncio
import aiopg
import psycopg2
import psycopg2.extras
import unittest

from aiopg.connection import Connection
from aiopg.cursor import Cursor


class TestConnection(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(None)

    def tearDown(self):
        self.loop.close()
        self.loop = None

    @asyncio.coroutine
    def connect(self, no_loop=False):
        loop = None if no_loop else self.loop
        return (yield from aiopg.connect(database='aiopg',
                                         user='aiopg',
                                         password='passwd',
                                         host='127.0.0.1',
                                         loop=loop))

    def test_connect(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            self.assertIsInstance(conn, Connection)

        self.loop.run_until_complete(go())

    def test_simple_select(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            cur = yield from conn.cursor()
            self.assertIsInstance(cur, Cursor)
            yield from cur.execute('SELECT 1')
            ret = yield from cur.fetchone()
            self.assertEqual((1,), ret)

        self.loop.run_until_complete(go())

    def test_default_event_loop(self):
        asyncio.set_event_loop(self.loop)

        @asyncio.coroutine
        def go():
            conn = yield from self.connect(no_loop=True)
            cur = yield from conn.cursor()
            self.assertIsInstance(cur, Cursor)
            yield from cur.execute('SELECT 1')
            ret = yield from cur.fetchone()
            self.assertEqual((1,), ret)

        self.loop.run_until_complete(go())

    def test_close(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            conn.close()
            self.assertIsNone(conn._conn)

        self.loop.run_until_complete(go())

    def test_close_twice(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            conn.close()
            conn.close()
            self.assertIsNone(conn._conn)

        self.loop.run_until_complete(go())

    def test_op_on_closed(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            cur = yield from conn.cursor()
            conn.close()
            with self.assertRaises(aiopg.ConnectionClosedError):
                yield from cur.execute('SELECT 1')

        self.loop.run_until_complete(go())

    def xtest_execute_twice(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            cur = yield from conn.cursor()
            #import ipdb;ipdb.set_trace()
            coro1 = cur.execute('SELECT 1')
            next(coro1)
            with self.assertRaises(RuntimeError):
                ret = yield from cur.execute('SELECT 2')
                self.assertEqual((2,), ret)

        self.loop.run_until_complete(go())

    def test_with_cursor_factory(self):

        @asyncio.coroutine
        def go():
            conn = yield from self.connect()
            cur = yield from conn.cursor(
                cursor_factory=psycopg2.extras.DictCursor)
            yield from cur.execute('SELECT 1 AS a')
            ret = yield from cur.fetchone()
            self.assertEqual(1, ret['a'])

        self.loop.run_until_complete(go())
