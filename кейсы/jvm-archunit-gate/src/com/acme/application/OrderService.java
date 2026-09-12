package com.acme.application;

import com.acme.domain.Order;

/** Слой приложения: работает с доменом (разрешённое направление). */
public class OrderService {
    public String summary(Order order) {
        return order == null ? "empty" : order.id();
    }
}
