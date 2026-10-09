import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs macOS or Linux")
