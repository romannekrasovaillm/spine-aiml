---
id: AD-13
type: ad
title: "Secrets management (Vault + HSM)"
status: "ADOPTED"
affects: [CMP-016]
verified_by: [C-009, C-016, C-022]
---

- **Binds**: все сервисы ↔ HashiCorp Vault, HSM
- **Prevents**: секреты в коде/configmaps/env vars — компрометация, отсутствие ротации, нарушение PCI DSS
- **Rule**: все секреты (БД-credentials, API-ключи рельсов, mTLS-ключи) — в HashiCorp Vault. Динамические credentials для PostgreSQL (Vault Database Engine): short-lived, auto-rotation. HSM — для криптографических ключей (PIN block encryption, card data crypto): ключи не покидают HSM, операции — через PKCS#11. Ротация: mTLS-сертификаты — 90 дней (cert-manager), БД-credentials — 1 час (dynamic), HSM keys — по графику PCI DSS. Запрещено хранение секретов в Kubernetes Secrets (кроме временных, для bootstrap Vault). Audit: Vault audit log — кто, когда, какой секрет запросил.
- **Статус**: [ADOPTED]
