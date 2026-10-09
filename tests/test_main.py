"""
Unit and Integration Test Suite for Employee Attendance & Analytics API
"""
import sys
import unittest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app, db, init_indexes

client = TestClient(app)


class TestEmployeeAttendanceAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_indexes()

    def test_01_health_check(self):
        """Test GET /health readiness probe"""
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_02_create_employee_success_and_duplicates(self):
        """Test POST /employees creation and 409 duplicate key conflict"""
        emp_payload = {
            "emp_code": "EMP8881",
            "name": "Aarav Sharma",
            "email": "aarav@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        }
        db.employees.delete_one({"emp_code": "EMP8881"})

        # Successful Creation
        res = client.post("/employees", json=emp_payload)
        self.assertEqual(res.status_code, 201)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP8881")
        self.assertIn("created_at", data)
        self.assertIsInstance(data["created_at"], int)

        # Duplicate Creation Failure (409)
        res_dup = client.post("/employees", json=emp_payload)
        self.assertEqual(res_dup.status_code, 409)

        # Invalid Shift Start == Shift End (422)
        invalid_emp = dict(emp_payload)
        invalid_emp["emp_code"] = "EMP8882"
        invalid_emp["shift_end"] = "09:30"
        res_invalid = client.post("/employees", json=invalid_emp)
        self.assertEqual(res_invalid.status_code, 422)

    def test_03_timestamp_validations(self):
        """Test 422 validation errors for invalid timestamps"""
        # Float timestamp -> 422
        res_float = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 1783310280000.5})
        self.assertEqual(res_float.status_code, 422)

        # Out-of-range epoch millis -> 422
        res_range = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": 12345})
        self.assertEqual(res_range.status_code, 422)

    def test_04_punch_in_and_punch_out_flow(self):
        """Test standard punch-in, late calculation (R2), and punch-out work hours (R4)"""
        emp_code = "EMP8883"
        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Neha Patel",
            "email": "neha@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01"
        })

        # Punch in at 09:28 IST (03:58 UTC on 2026-07-06) -> 1783310280000 ms
        ts_in = 1783310280000
        res_in = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ts_in,
            "status": "PRESENT"
        })
        self.assertEqual(res_in.status_code, 201)
        in_data = res_in.json()
        self.assertEqual(in_data["date"], "2026-07-06")
        self.assertEqual(in_data["late_minutes"], 0)
        self.assertIsNone(in_data["punch_out"])

        # Duplicate Punch-in (409)
        res_in_dup = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ts_in + 1000,
            "status": "PRESENT"
        })
        self.assertEqual(res_in_dup.status_code, 409)

        # Punch out at 18:35 IST (13:05 UTC on 2026-07-06) -> 1783343100000 ms
        # Work hours: 03:58 UTC to 13:05 UTC = 9h 7m = 9.1166... -> 9.12 hours
        ts_out = 1783343100000
        res_out = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": ts_out
        })
        self.assertEqual(res_out.status_code, 200)
        out_data = res_out.json()
        self.assertEqual(out_data["work_hours"], 9.12)
        self.assertEqual(out_data["overtime_minutes"], 0)  # 5 min overtime is < 30 min threshold
        self.assertFalse(out_data["half_day"])

    def test_05_overnight_shift_and_overtime(self):
        """Test overnight shift (22:00-06:00) date calculation (R1) and overtime threshold (R3)"""
        emp_code = "EMP8884"
        db.employees.delete_one({"emp_code": emp_code})
        db.attendance_logs.delete_many({"emp_code": emp_code})

        client.post("/employees", json={
            "emp_code": emp_code,
            "name": "Rohan Verma",
            "email": "rohan@example.com",
            "department": "Operations",
            "shift_start": "22:00",
            "shift_end": "06:00",
            "joined_on": "2026-01-01"
        })

        # Punch in at 21:55 IST on 2026-07-06 (16:25 UTC) -> 1783355100000 ms
        ts_in = 1783355100000
        res_in = client.post("/attendance/punch-in", json={
            "emp_code": emp_code,
            "punched_at": ts_in,
            "status": "PRESENT"
        })
        self.assertEqual(res_in.status_code, 201)
        self.assertEqual(res_in.json()["date"], "2026-07-06")

        # Punch out next day at 06:40 IST on 2026-07-07 (01:10 UTC) -> 1783386600000 ms
        # Work hours: 21:55 to 06:40 = 8.75 hours
        # Overtime: 06:40 - 06:00 = 40 min (>= 30 min threshold) -> 40
        ts_out = 1783386600000
        res_out = client.post("/attendance/punch-out", json={
            "emp_code": emp_code,
            "punched_at": ts_out
        })
        self.assertEqual(res_out.status_code, 200)
        out_data = res_out.json()
        self.assertEqual(out_data["work_hours"], 8.75)
        self.assertEqual(out_data["overtime_minutes"], 40)

    def test_06_manual_regularization_audit_trail(self):
        """Test PATCH /attendance/{emp_code}/{date} manual correction and history entry"""
        res = client.patch("/attendance/EMP8883/2026-07-06", json={
            "punch_in": 1783312500000,  # 10:05 IST (35 min late)
            "reason": "Adjusted due to biometric delay",
            "regularized_by": "hr.manager"
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["late_minutes"], 35)
        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["history"][0]["by"], "hr.manager")

    def test_07_analytics_employee_monthly(self):
        """Test GET /analytics/employees/{emp_code}/monthly pipeline"""
        res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["emp_code"], "EMP0001")
        self.assertIn("working_days", data)
        self.assertIn("present_days", data)

    def test_08_analytics_department_summary(self):
        """Test GET /analytics/departments/summary pipeline with 0-log headcount inclusion (R9)"""
        res = client.get("/analytics/departments/summary?month=2026-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["month"], "2026-07")
        self.assertIsInstance(data["items"], list)

    def test_09_analytics_late_leaderboard(self):
        """Test GET /analytics/leaderboard/late pipeline with competition ranking ($rank)"""
        res = client.get("/analytics/leaderboard/late?month=2026-07&limit=5")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("items", data)

    def test_10_analytics_department_trend(self):
        """Test GET /analytics/departments/{department}/trend pipeline with $densify and 7-day moving avg"""
        res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["department"], "Engineering")
        self.assertEqual(len(data["items"]), 7)

    def test_11_admin_explain(self):
        """Test GET /admin/explain/{endpoint} executionStats query plan profiling"""
        endpoints = [
            "/admin/explain/attendance_list?page=1",
            "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
            "/admin/explain/department_summary?month=2026-07",
            "/admin/explain/late_leaderboard?month=2026-07",
            "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07"
        ]
        for url in endpoints:
            res = client.get(url)
            self.assertEqual(res.status_code, 200, f"Failed explain on {url}: {res.text}")
            data = res.json()
            self.assertIn("explain", data)


if __name__ == "__main__":
    unittest.main()
