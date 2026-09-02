import doctest

import levers._core


def test_the_module_docstring_examples_run():
    r = doctest.testmod(levers._core, verbose=False)
    assert r.failed == 0
