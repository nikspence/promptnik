# Promptnik branding

`promptnik-light.svg` and `promptnik-dark.svg` are transparent, path-based traces of the supplied root PNGs. The SVGs contain no embedded raster images or font dependencies. Shading is approximated with vector color regions. Matching RGBA PNGs are rendered from those SVGs.

`promptnik-icon.svg` uses the robot mark, cropped through the SVG viewBox. Its transparent PNG and multi-resolution Windows ICO are intended for app/window icons.

The runtime uses the SVGs. Packaging includes all SVG, PNG, and ICO assets. The original root PNGs are preserved.

To rebuild assets, run `python tools/trace_branding.py` from an environment with Pillow, NumPy, and PySide6. These asset-build dependencies do not add runtime dependencies to Promptnik.

Built-in imagegen background-removal trials were evaluated first, but were not shipped because of rough lettering edges. The final assets use the deterministic vector trace above.
