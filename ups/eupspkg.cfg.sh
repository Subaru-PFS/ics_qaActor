config() { :; }

install() {
    echo "==> [EUPS] Building and installing via pip..."

    # 1. Install using pip as normal (places code in lib/python3.X/site-packages)
    pip install . \
        --prefix="$PREFIX" \
        --no-deps \
        --no-build-isolation

    # 2. Bridge the modern pip path to the legacy EUPS path
    # Find the specific python3.X folder pip just created inside lib/
    PY_VER_DIR=$(find "$PREFIX/lib" -maxdepth 1 -type d -name "python3.*" | head -n 1)

    if [ -n "$PY_VER_DIR" ]; then
        # Navigate to the lib directory
        cd "$PREFIX/lib"

        # Symlink e.g., "python3.12/site-packages" to "python"
        # Now, $PREFIX/lib/python safely points to the actual code.
        ln -s "$(basename "$PY_VER_DIR")/site-packages" python
    else
        echo "Warning: Could not find Python installation directory in $PREFIX/lib"
    fi
}
