import os

import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ============================================================
# Collaboration smoke tests (read-only)
# Same structure as student_registration_smoke.py.
# No database access, no credentials, no data is created.
#
#   pytest -v collaboration_smoke.py
#
# Maintenance checks only run when EXPECT_MAINTENANCE_NOTICE=true
# (turn collaboration maintenance ON first):
#   POST /api/collaboration/maintenance/on
# ============================================================

BASE_URL = os.getenv(
    "CHAKORAHUB_BASE_URL",
    "https://www.chakorahub.com",
).rstrip("/")

APPLICATION_URL = f"{BASE_URL}/applicationform"
BRS_URL = f"{BASE_URL}/industry/brs"
SIGNOFF_URL = f"{BASE_URL}/signoff-form"
TRACK_PROJECT_URL = f"{BASE_URL}/track-project"
PROJECT_DASHBOARD_URL = f"{BASE_URL}/project-dashboard"
MAINTENANCE_STATUS_PATH = "/api/collaboration-maintenance/status"

WAIT = 30


@pytest.fixture
def driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1440,1100")

    browser = webdriver.Chrome(options=options)
    browser.set_page_load_timeout(60)
    yield browser
    browser.quit()


def wait_loaded(driver):
    WebDriverWait(driver, WAIT).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def wait_for_element(driver, by, value):
    return WebDriverWait(driver, WAIT).until(
        EC.visibility_of_element_located((by, value))
    )


def maintenance_expected():
    return os.getenv("EXPECT_MAINTENANCE_NOTICE", "false").lower() == "true"


# ============================================================
# SMOKE TESTS
# ============================================================

def test_application_page_loads(driver):
    driver.get(APPLICATION_URL)
    wait_loaded(driver)

    assert "/applicationform" in driver.current_url
    assert driver.find_element(By.TAG_NAME, "body").is_displayed()


def test_application_form_controls_present(driver):
    driver.get(APPLICATION_URL)
    wait_loaded(driver)

    for field_name in (
        "full_name",
        "email",
        "phone",
        "organisation",
        "collaboration_type",
        "project_title",
        "description",
    ):
        assert driver.find_element(By.NAME, field_name).is_displayed()


def test_application_form_required_validation(driver):
    driver.get(APPLICATION_URL)
    wait_loaded(driver)

    form = driver.find_element(By.CSS_SELECTOR, "form")
    is_valid = driver.execute_script("return arguments[0].checkValidity();", form)

    assert is_valid is False


def test_brs_page_loads_and_controls(driver):
    driver.get(BRS_URL)
    wait_loaded(driver)

    assert "/industry/brs" in driver.current_url

    for field_name in ("project_id", "project_name", "client_name", "file"):
        assert driver.find_element(By.NAME, field_name).is_displayed()


def test_signoff_page_loads(driver):
    driver.get(SIGNOFF_URL)
    wait_loaded(driver)

    assert "/signoff-form" in driver.current_url
    assert driver.find_element(By.TAG_NAME, "body").is_displayed()


def test_track_project_page_loads(driver):
    driver.get(TRACK_PROJECT_URL)
    wait_loaded(driver)

    assert "/track-project" in driver.current_url
    assert driver.find_element(By.TAG_NAME, "body").is_displayed()


def test_track_project_controls_present(driver):
    driver.get(TRACK_PROJECT_URL)
    wait_loaded(driver)

    assert driver.find_element(By.ID, "projectId").is_displayed()
    assert driver.find_element(
        By.XPATH, "//button[contains(normalize-space(), 'Track Status')]"
    ).is_displayed()


def test_project_dashboard_page_loads(driver):
    driver.get(PROJECT_DASHBOARD_URL)
    wait_loaded(driver)

    assert "/project-dashboard" in driver.current_url
    assert driver.find_element(By.TAG_NAME, "body").is_displayed()


# ============================================================
# MAINTENANCE MODE
# ============================================================

def test_collaboration_maintenance_status_api(driver):
    """The public status endpoint must answer with a maintenance_mode flag."""
    driver.get(APPLICATION_URL)
    wait_loaded(driver)

    result = driver.execute_async_script(
        """
        const path = arguments[0];
        const done = arguments[arguments.length - 1];

        fetch(path)
        .then(async response => {
            let data = {};
            try { data = await response.json(); } catch (e) {}
            done({status: response.status, data: data});
        })
        .catch(error => done({status: 0, data: {message: String(error)}}));
        """,
        MAINTENANCE_STATUS_PATH,
    )

    assert "maintenance_mode" in result["data"], result

    if maintenance_expected():
        assert result["status"] == 200, result
        assert result["data"]["maintenance_mode"] is True, result


def test_collaboration_maintenance_notice(driver):
    """Verify the Collaboration maintenance notice when deployment expects it."""
    if not maintenance_expected():
        pytest.skip("Maintenance notice is not expected for this run.")

    driver.get(APPLICATION_URL)
    wait_loaded(driver)

    notice = WebDriverWait(driver, WAIT).until(
        EC.visibility_of_element_located(
            (By.ID, "collaboration-maintenance-notice")
        )
    )

    assert "Scheduled maintenance notice" in notice.text
    assert "Collaboration service" in notice.text
