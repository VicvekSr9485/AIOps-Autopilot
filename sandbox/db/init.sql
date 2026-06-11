-- Runs once on a fresh volume (reset = down -v, so every reset reseeds).
CREATE ROLE app LOGIN PASSWORD 'app_pw';

CREATE TABLE items (
    id serial PRIMARY KEY,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON items TO app;
GRANT USAGE ON SEQUENCE items_id_seq TO app;
