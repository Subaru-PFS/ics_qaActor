install()
{
    if test -z "$SCONSUTILS_DIR"; then
        echo disabling scons because we do not support it.
        mv SConstruct SConstruct-disabled
    fi
    default_install "$@"

    # Find the specific python3.X folder pip just created inside lib/
    PY_VER_DIR=$(find "$PREFIX/lib" -maxdepth 1 -type d -name "python.*" | head -n 1)

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
