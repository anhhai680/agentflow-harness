import unittest

from toy import greet


class GreetTest(unittest.TestCase):
    def test_greets_by_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_greets_the_world_by_default(self):
        self.assertEqual(greet(), "Hello, world!")
