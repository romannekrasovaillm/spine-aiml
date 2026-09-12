package com.acme.domain;

/**
 * Исправленный домен (кейс 009): без зависимостей на инфраструктуру (C-01)
 * и «внешний FX-пакет» (C-02) — чистая сущность предметной области.
 */
public class Order {
    private final String id;

    public Order(String id) {
        this.id = id;
    }

    public String id() {
        return id;
    }
}
