// @ts-check
const { test, expect } = require('@playwright/test');

const BASE = 'http://localhost:8000';

test.describe('MarketSync — основной UI', () => {

  test('страница открывается, боковое меню видно', async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator('.logo-t')).toHaveText('MarketSync');
    await expect(page.locator('button.ni').first()).toBeVisible();
  });

  test('ключи подгружаются из /api/config', async ({ page }) => {
    await page.goto(BASE);
    // Ждём пока loadSettings() завершится
    await page.waitForTimeout(1500);
    // Хотя бы МС-токен должен быть заполнен
    const val = await page.inputValue('#cfg-ms-token');
    expect(val.length).toBeGreaterThan(0);
  });

  test('навигация по страницам работает', async ({ page }) => {
    await page.goto(BASE);
    // Загрузить товары
    await page.click('button.ni:has-text("Загрузить товары")');
    await expect(page.locator('#pg-add')).toHaveClass(/active/);
    // Низкий рейтинг
    await page.click('button.ni:has-text("Низкий рейтинг")');
    await expect(page.locator('#pg-lowrated')).toHaveClass(/active/);
    // Настройки
    await page.click('button.ni:has-text("API-ключи")');
    await expect(page.locator('#pg-settings')).toHaveClass(/active/);
    // Обзор
    await page.click('button.ni:has-text("Обзор")');
    await expect(page.locator('#pg-dashboard')).toHaveClass(/active/);
  });

  test('проверка подключений (кнопка "Проверить")', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(1500);
    await page.click('button:has-text("Проверить подключение")');
    // Ждём хотя бы один ответ от API
    await page.waitForTimeout(5000);
    // Должен появиться статус в боковой панели
    const msStatus = await page.textContent('#s-ms');
    expect(msStatus).not.toBe('нет ключа');
  });

  test('страница "Загрузить товары" — шаг 1: ввод кода', async ({ page }) => {
    await page.goto(BASE);
    await page.click('button.ni:has-text("Загрузить товары")');
    await expect(page.locator('#pg-add')).toHaveClass(/active/);
    // Поле ввода кодов должно быть видно
    const textarea = page.locator('textarea[placeholder*="18406"], textarea[placeholder*="код"], #add-codes');
    await expect(textarea.first()).toBeVisible();
  });

  test('загрузка товара по коду 18406', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(1500);
    await page.click('button.ni:has-text("Загрузить товары")');

    // Вводим код товара
    const textarea = page.locator('textarea').first();
    await textarea.fill('18406');

    // Нажимаем "Загрузить"
    await page.click('button:has-text("Загрузить данные")');

    // Ждём карточку товара
    await expect(page.locator('[data-add-code="18406"]')).toBeVisible({ timeout: 15000 });

    // Карточка содержит название
    const cardTitle = await page.textContent('[data-add-code="18406"] .card-t');
    expect(cardTitle?.length).toBeGreaterThan(3);
  });

  test('шаг 2: карточка товара содержит бренд и описание (без категорий)', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(1500);
    await page.click('button.ni:has-text("Загрузить товары")');

    const textarea = page.locator('textarea').first();
    await textarea.fill('18406');
    await page.click('button:has-text("Загрузить данные")');
    await expect(page.locator('[data-add-code="18406"]')).toBeVisible({ timeout: 15000 });

    // Поле "Бренд" есть
    await expect(page.locator('[data-add-code="18406"] [data-add-field="brand"]')).toBeVisible();
    // Поле "Описание" есть
    await expect(page.locator('[data-add-code="18406"] [data-add-field="description"]')).toBeVisible();
    // Категорий НЕТ (убрали в прошлой сессии)
    const catSelectors = await page.locator('[data-add-code="18406"] [id^="dc-casc-ym-"]').count();
    expect(catSelectors).toBe(0);
  });

  test('настройки: ключи заполнены и сохраняются', async ({ page }) => {
    await page.goto(BASE);
    await page.waitForTimeout(1500);
    await page.click('button.ni:has-text("API-ключи")');

    const msToken = await page.inputValue('#cfg-ms-token');
    const ymKey   = await page.inputValue('#cfg-ym-key');
    const ozId    = await page.inputValue('#cfg-oz-client');
    const ozKey   = await page.inputValue('#cfg-oz-key');

    expect(msToken.length).toBeGreaterThan(0);
    expect(ymKey.length).toBeGreaterThan(0);
    expect(ozId.length).toBeGreaterThan(0);
    expect(ozKey.length).toBeGreaterThan(0);
  });

});

// ════════════════════════════════════════════════════════════════
// WB card logic — API-level checks
// ════════════════════════════════════════════════════════════════

test.describe('WB — preview API', () => {

  test('wb/preview returns expected shape for empty codes list', async ({ request }) => {
    const resp = await request.post(`${BASE}/api/wb/preview`, {
      data: { ms_token: '', wb_api_key: '', codes: [] },
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body).toHaveProperty('results');
    expect(body).toHaveProperty('summary');
  });

});

// ════════════════════════════════════════════════════════════════
// Ozon characteristics cache — API-level checks
// ════════════════════════════════════════════════════════════════

test.describe('Ozon — characteristics cache', () => {

  test('/api/ozon/category/attributes returns type_id error for key without underscore', async ({ request }) => {
    const resp = await request.post(`${BASE}/api/ozon/category/attributes`, {
      data: { ozon_client_id: '', ozon_api_key: '', category_key: '12345' },
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.ok).toBe(false);
    expect(body.error).toContain('тип товара');
  });

  test('/api/ozon/category/attributes returns parse error for non-numeric key', async ({ request }) => {
    const resp = await request.post(`${BASE}/api/ozon/category/attributes`, {
      data: { ozon_client_id: '', ozon_api_key: '', category_key: 'abc_xyz' },
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.ok).toBe(false);
  });

});

// ════════════════════════════════════════════════════════════════
// WB detail-preview endpoint shape
// ════════════════════════════════════════════════════════════════

test.describe('WB — offer detail preview', () => {

  test('wb/offer/detail-preview returns ok and results for empty codes', async ({ request }) => {
    const resp = await request.post(`${BASE}/api/wb/offer/detail-preview`, {
      data: { ms_token: '', wb_api_key: '', codes: [] },
    });
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body).toHaveProperty('ok');
    expect(body).toHaveProperty('results');
  });

});
