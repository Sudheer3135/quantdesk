-- Proposed role for the power controller (NOT APPLIED). Run by the cluster
-- superuser under separate authorization; the password is supplied at run
-- time (psql -v pw=...) and written only to /etc/quantdesk-power/pgpass.
--
-- It can read two columns, take QuantDesk's writer key (advisory-lock
-- functions are available to every role) and count sessions in
-- pg_stat_activity (visible to every role). It cannot write.
CREATE ROLE quantdesk_power LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS NOINHERIT CONNECTION LIMIT 1 PASSWORD :'pw';
ALTER ROLE quantdesk_power SET default_transaction_read_only = on;
ALTER ROLE quantdesk_power SET statement_timeout = '10s';
ALTER ROLE quantdesk_power SET lock_timeout = '1s';
GRANT CONNECT ON DATABASE quantdesk TO quantdesk_power;
GRANT USAGE ON SCHEMA public TO quantdesk_power;
GRANT SELECT (status) ON paper_positions TO quantdesk_power;
GRANT SELECT (status) ON trades TO quantdesk_power;
