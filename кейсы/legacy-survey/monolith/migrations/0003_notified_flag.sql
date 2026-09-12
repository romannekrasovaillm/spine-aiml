-- 0003: флаг доставки уведомления (писали в проде руками, миграцию догнали позже).
ALTER TABLE payments ADD COLUMN notified boolean NOT NULL DEFAULT false;
