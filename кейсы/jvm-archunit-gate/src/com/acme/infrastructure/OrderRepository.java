package com.acme.infrastructure;

/** Инфраструктура: хранилище заказов. */
public class OrderRepository {
    public String find(String id) {
        return "order:" + id;
    }
}
