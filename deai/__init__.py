"""Django app 包。

真正的引擎在 ``deai.engine``，它不依赖 Django、不联网，可以脱离项目单独测试::

    cd server && python -m unittest discover -s tests -t . -v
"""
