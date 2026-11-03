-- Usuário somente leitura usado pela API.
-- Camada extra de segurança, além da validação do SQL e da transação READ ONLY:
-- mesmo que uma consulta perigosa passasse, o banco recusaria qualquer escrita.
-- O script pode ser executado mais de uma vez sem erro.
-- Em produção, troque a senha e leia-a de um secret.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sales_reader') THEN
        CREATE ROLE sales_reader LOGIN PASSWORD 'sales_reader';
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO sales_reader', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO sales_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sales_reader;
GRANT SELECT ON products, customers, sales TO sales_reader;

ALTER ROLE sales_reader SET default_transaction_read_only = on;
ALTER ROLE sales_reader SET statement_timeout = '5s';
