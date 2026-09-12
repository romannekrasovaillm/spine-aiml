---
id: AD-1
type: ad
title: "Saga Orchestration для жизненного цикла платежа"
status: "ADOPTED"
affects: [CMP-002]
verified_by: [C-008]
---

- **Binds**: Payment Orchestrator ↔ все участники платежного потока (Authorization, Fraud, AML, Rail Connectors, Ledger)
- **Prevents**: расхождение «кто отвечает за последовательность шагов платежа» — без оркестратора каждый участник решает сам, поток невидим, компенсации не скоординированы
- **Rule**: жизненный цикл платежа управляется **только** оркестратором (Saga Execution Coordinator). Состояние саги персистентно (таблица `payment_saga`), явная state machine. Компенсации — семантические (сторно, возврат), не удаление. Хореография запрещена для потоков >3 участников.
- **Статус**: [ADOPTED]
