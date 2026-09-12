package com.acme.domain;

import com.acme.infrastructure.OrderRepository;
import com.external.fx.FxClient;

/**
 * НАМЕРЕННОЕ НАРУШЕНИЕ (кейс 009): домен импортирует инфраструктуру (C-01)
 * и «внешний FX-пакет» (C-02). Исправленная версия — в fixed/.
 */
public class Order {
    private final String id;
    private final OrderRepository repository;
    private final FxClient fxClient;

    public Order(String id, OrderRepository repository, FxClient fxClient) {
        this.id = id;
        this.repository = repository;
        this.fxClient = fxClient;
    }

    public String id() {
        return id;
    }

    public String load() {
        return repository.find(id);
    }

    public String fxRate() {
        return fxClient.rate(id);
    }
}
