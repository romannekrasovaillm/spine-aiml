-- Ad-hoc отчёт для правления: оборот по дням (запускают руками из psql).
SELECT date_trunc('day', created_at) AS day,
       currency,
       count(*)        AS payments_cnt,
       sum(amount)     AS turnover
FROM payments
GROUP BY 1, 2
ORDER BY 1 DESC;
