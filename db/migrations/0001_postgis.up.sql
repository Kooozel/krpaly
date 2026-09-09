-- PostGIS is what makes the geography columns in 0004 possible. `if not
-- exists` because every image this runs against already provides it; the
-- migration exists so a bare postgres:17 is also bootstrappable.
create extension if not exists postgis;
