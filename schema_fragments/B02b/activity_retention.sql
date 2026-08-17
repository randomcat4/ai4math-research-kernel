CREATE TABLE product_activity_retention(
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  first_available_cursor INTEGER NOT NULL CHECK(first_available_cursor >= 1),
  updated_at TEXT NOT NULL
) STRICT;

INSERT INTO product_activity_retention(singleton, first_available_cursor, updated_at)
VALUES(1, 1, '1970-01-01T00:00:00Z');