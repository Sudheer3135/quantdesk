"""The historical data layer.

Everything that writes to or reads from the historical tables goes through
here. The rule this package exists to enforce: a row in the database can
always answer where it came from, when it was captured, and whether it was
observed or derived.

Modules, in the order data moves through them:

    validation → importer → (postgres) → repository → dataset

`quality` inspects what landed. `upsert` is the write primitive the
importer is built on.
"""
