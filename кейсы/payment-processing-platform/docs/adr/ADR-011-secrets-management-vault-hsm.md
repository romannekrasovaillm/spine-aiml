# ADR-011. Secrets management — Vault + HSM

- Date: 2026-08-15
- Status: Accepted

## Context

Платформа хранит и использует секреты: credentials к БД (10+ инстансов), API-ключи рельсов (NSPK, CBR, SWIFT, Core Banking), mTLS-ключи, криптографические ключи для карт (PIN block, PAN encryption). Хранение в Kubernetes Secrets/env vars — компрометация при доступе к кластеру, отсутствие ротации, нарушение PCI DSS Requirement 3 (protect stored cardholder data) и Requirement 8 (authentication). HSM требуется для криптоопераций с картами (PCI DSS Requirement 3.5).

## Decision

**HashiCorp Vault для secrets + HSM для криптографических ключей.**

### Vault — all secrets
- **Dynamic credentials для PostgreSQL**: Vault Database Engine выдаёт short-lived credentials (TTL=1h, max_ttl=24h) каждому сервису. Авто-rotation, отзыв при остановке пода.
- **Static secrets** (API-ключи рельсов): хранятся в Vault KV, rotation по графику (90 дней), уведомление за 7 дней до истечения.
- **mTLS-сертификаты**: cert-manager + Vault PKI, rotation 90 дней, автоматическая.
- **Auth**: Kubernetes Service Account JWT → Vault Kubernetes auth method. Каждый под получает токен при старте.
- **Audit**: Vault audit log — кто, когда, какой секрет запросил. Log — в SIEM.

### HSM — криптографические ключи
- **Назначение**: PIN block encryption/decryption, PAN encryption/decryption (tokenization), MAC для ISO 8583.
- **Доступ**: через PKCS#11 interface. Ключи никогда не покидают HSM.
- **Key lifecycle**: generation в HSM, rotation по графику PCI DSS (годовой для data encryption keys). Старые ключи — для расшифровки исторических данных (key versioning).
- **HA**: HSM cluster (2+ устройства), синхронизация ключей.

### Что НЕ хранится в Vault
- Configuration (non-secret) — ConfigMaps.
- Feature flags — отдельный service (AD-24).
- Kubernetes Secrets — только bootstrap token для Vault auth (получение при старте пода).

## Alternatives Considered

| Вариант | Плюсы | Минусы |
|---------|-------|--------|
| **Kubernetes Secrets (encrypted at rest)** | Просто, встроено. | Нет dynamic credentials. Нет audit. Нет rotation policy. Нарушение PCI DSS. |
| **Cloud KMS** | Без инфраструктуры. | Данные КИИ в облаке — 187-ФЗ. |
| **手动 rotation** | Нет инструментов. | Человеческий фактор. Задержка rotation. |

## Consequences

### Positive

- PCI DSS compliance (Requirement 3, 8, 12).
- Auto-rotation — секреты не «живут» долго.
- Audit trail — кто получил доступ к какому секрету.
- Dynamic DB credentials — компрометация пода = компрометация только short-lived token.

### Negative

- Vault — новый критический компонент (SPOF). Митигация: HA-кластер (3 ноды), raft storage.
- HSM — инфраструктурная зависимость, длинный цикл закупки. Начать до H0.
- Задержка: каждый под делает запрос к Vault при старте (~100ms).

## Reversibility

**Costly.** Уход от Vault — миграция всех секретов, изменение auth-механизма для всех сервисов. HSM — практически необратим (купленное оборудование).

## References

- Spine: AD-13, AD-6 (trust zones)
- Регулятор: PCI DSS Req 3, 8, 12; 187-ФЗ
