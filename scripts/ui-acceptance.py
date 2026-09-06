#!/usr/bin/env python3
"""Visual and interaction acceptance against scripts/ui-demo.py (simulated hardware)."""
import pathlib
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROUTES = ('overview', 'fans', 'curves', 'sensors', 'analytics', 'automation', 'settings', 'diagnostics')
options = webdriver.ChromeOptions()
options.binary_location = '/usr/bin/chromium'
for option in ('--headless', '--no-sandbox', '--disable-dev-shm-usage'):
    options.add_argument(option)
options.set_capability('goog:loggingPrefs', {'browser': 'ALL'})
driver = webdriver.Chrome(service=Service('/usr/bin/chromedriver'), options=options)
wait = WebDriverWait(driver, 15)


def page(route, state=''):
    driver.get(f'http://127.0.0.1:5173/?state={state}#/{route}')
    if state not in ('disconnected', 'unsupported'):
        wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, 'h1'), route.title()))
    time.sleep(.4)


def button(text):
    return driver.find_element(By.XPATH, f'//button[normalize-space()="{text}"]')


def select(label, value):
    control = Select(driver.find_element(By.XPATH, f'//label[span="{label}"]/select'))
    option = next(option for option in control.options if option.get_attribute('value') == value)
    control.select_by_visible_text(option.text)
    time.sleep(.3)


def toggle(label):
    driver.find_element(By.XPATH, f'//label[span="{label}"]/input[@type="checkbox"]').click()
    time.sleep(.3)


def screenshot(name):
    width, scroll = driver.execute_script('return [innerWidth, document.documentElement.scrollWidth]')
    assert scroll <= width, f'Horizontal overflow on {name}: {scroll} > {width}'
    driver.save_screenshot(str(ROOT / 'docs/images/v2' / f'{name}.png'))


try:
    driver.set_window_size(1280, 900)
    if '--interactions-only' not in sys.argv:
        for theme in ('dark', 'light'):
            for route in ROUTES:
                page(route, 'light' if theme == 'light' else '')
                screenshot(f'{route}-{theme}')
                print(f'PASS {route} {theme}', flush=True)
        driver.set_window_size(390, 844)
        for route in ROUTES:
            page(route)
            screenshot(f'{route}-narrow')
        driver.set_window_size(1280, 900)
        for state in ('warning', 'critical', 'stale', 'disconnected', 'unsupported', 'missing'):
            page('automation' if state == 'missing' else 'overview', state)
            if state == 'stale':
                time.sleep(6.5)
                assert 'stale' in driver.find_element(By.TAG_NAME, 'body').text.lower()
            screenshot(state)

    page('fans')
    name = driver.find_element(By.XPATH, '//label[span="Fan name"]/input')
    name.send_keys(Keys.CONTROL, 'a')
    name.send_keys('CPU acceptance')
    button('Save fan policy').click()
    wait.until(EC.presence_of_element_located((By.XPATH, '//h2[text()="CPU acceptance"]')))
    button('Test +10% for 5 seconds').click()
    wait.until(EC.text_to_be_present_in_element((By.CLASS_NAME, 'toast'), 'five seconds'))
    page('curves')
    button('Create curve').click()
    wait.until(EC.presence_of_element_located((By.XPATH, '//button[contains(., "New curve")]'))).click()
    button('Insert midpoint').click()
    button('Save curve').click()
    wait.until(EC.text_to_be_present_in_element((By.CLASS_NAME, 'toast'), 'Changes saved'))
    button('Duplicate curve').click()
    wait.until(EC.presence_of_element_located((By.XPATH, '//button[contains(., "New curve copy")]')))
    page('automation')
    button('Create rule').click()
    wait.until(EC.visibility_of_element_located((By.TAG_NAME, 'dialog')))
    driver.find_element(By.XPATH, '//label[span="Rule name"]/input').send_keys('Acceptance rule')
    button('Test rule').click()
    wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, 'dialog'), 'No action applied'))
    button('Save rule').click()
    wait.until(EC.invisibility_of_element_located((By.TAG_NAME, 'dialog')))
    page('settings')
    theme = driver.find_element(By.XPATH, '//label[span="Theme"]/select')
    Select(theme).select_by_value('light')
    wait.until(lambda d: d.execute_script('return document.documentElement.dataset.theme') == 'light')
    print('PASS fan rename/test, curve create/edit/duplicate, rule test/save, theme mutation', flush=True)

    select('Display temperatures', 'f')
    select('Keep history', '14')
    for label in ('Desktop notifications', 'Critical-temperature alerts', 'Persist telemetry to disk', 'Collapse sidebar'):
        toggle(label)
        toggle(label)
    safety = driver.find_element(By.XPATH, '//label[span="Hysteresis (%)"]/input')
    safety.send_keys(Keys.CONTROL, 'a')
    safety.send_keys('6')
    button('Save safety settings').click()
    time.sleep(.3)
    button('Return to EC Auto').click()
    wait.until(EC.visibility_of_element_located((By.TAG_NAME, 'dialog')))
    driver.switch_to.active_element.send_keys(Keys.ESCAPE)
    wait.until(EC.invisibility_of_element_located((By.TAG_NAME, 'dialog')))
    page('overview')
    select('Configured profile', 'balanced')
    select('Configured mode', 'manual')
    driver.find_element(By.TAG_NAME, 'body').send_keys(']')
    time.sleep(.3)
    select('Configured mode', 'auto')
    driver.find_element(By.TAG_NAME, 'body').send_keys(Keys.CONTROL, 'k')
    wait.until(EC.visibility_of_element_located((By.TAG_NAME, 'dialog')))
    button('Open Sensors').click()
    wait.until(EC.text_to_be_present_in_element((By.TAG_NAME, 'h1'), 'Sensors'))
    driver.find_elements(By.XPATH, '//button[text()="CPU"]')[0].click()
    time.sleep(.3)
    driver.find_elements(By.XPATH, '//button[text()="GPU"]')[-1].click()
    time.sleep(.3)
    pin = driver.find_elements(By.XPATH, '//label[span="Pin to history"]/input')[0]
    pin.click()
    time.sleep(.3)
    button('Use automatic CPU / GPU selection').click()
    page('analytics')
    for value in ('3600', '86400', '604800', 'live'):
        select('Time range', value)
    assert 'fan3 duty' not in driver.find_element(By.TAG_NAME, 'body').text
    page('automation')
    button('Add schedule block').click()
    toggle('Enable schedule')
    button('Save schedule').click()
    time.sleep(.3)
    button('Remove block').click()
    button('Save schedule').click()
    page('diagnostics')
    select('Filter fan', 'fan2')
    button('Refresh diagnostics').click()
    print('PASS safety, preferences, keyboard palette/Escape, modes, sensor selection/pinning, history ranges, schedule, diagnostics filter', flush=True)
    errors = [entry for entry in driver.get_log('browser') if entry['level'] == 'SEVERE' and 'favicon' not in entry['message']]
    assert not errors, errors
except Exception:
    print(driver.find_element(By.TAG_NAME, 'body').text, flush=True)
    driver.save_screenshot('/tmp/fan-acceptance-failure.png')
    raise
finally:
    driver.quit()
