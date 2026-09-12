---
id: AD-6
type: ad
title: "Сегментация доверенных зон (4 зоны)"
status: "ADOPTED"
affects: [CMP-001, CMP-006]
verified_by: [C-006, C-007, C-021]
---

- **Binds**: все компоненты платформы ↔ network segmentation, mTLS, firewall rules
- **Prevents**: проброс атаки из публичного канала к финансовым данным, нарушение PCI DSS / КИИ
- **Rule**: 4 изолированные зоны — (1) DMZ: API Gateway, channel adapters; (2) Application: Orchestrator, Auth, Fraud, AML; (3) Data: Ledger, Clearing; (4) Integration: Rail Connectors. Междузонный трафик — mTLS + network policy. Синхронный вызов через зону — только через service mesh. Пан-данные (номер карты) не покидают Data Zone (токенизация в DMZ).
- **Статус**: [ADOPTED]
