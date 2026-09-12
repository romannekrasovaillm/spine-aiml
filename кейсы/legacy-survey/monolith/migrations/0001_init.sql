-- 0001: исходная схема монолита.
CREATE TABLE payments (
    id          bigserial PRIMARY KEY,
    account_id  bigint       NOT NULL,
    amount      numeric(14,2) NOT NULL,
    currency    char(3)      NOT NULL DEFAULT 'RUB',
    created_at  timestamptz  NOT NULL DEFAULT now()
);
