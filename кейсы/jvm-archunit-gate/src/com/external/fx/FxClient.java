package com.external.fx;

/** Синтетический «внешний FX-пакет»: напрямую использовать запрещено (C-02). */
public class FxClient {
    public String rate(String pair) {
        return "fx:" + pair;
    }
}
