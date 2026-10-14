"""Peak-finder internals: the analysis (blur + argmax) and the ellipsoid builder.

Kept out of ``peak_roi.py`` so both halves can be exercised offline by
``selftest.py`` without a host, a socket, or a shared segment.
"""
