"""Many parents checking results at the same time."""
import random
from locust import HttpUser, task, between


class Parent(HttpUser):
    wait_time = between(2, 6)

    def on_start(self):
        n = random.randint(0, 299)
        self.child = n + 1
        self.client.get("/login/", name="GET /login/")
        tok = self.client.cookies.get("csrftoken")
        self.client.post("/login/", data={"username": f"parent{n:03d}", "password": "Str0ng-Pass!",
                                          "csrfmiddlewaretoken": tok},
                         headers={"Referer": self.host + "/login/"}, name="POST /login/")

    @task(3)
    def portal(self):
        self.client.get("/parent/", name="/parent/ (my children)")

    @task(5)
    def reports(self):
        self.client.get(f"/parent/children/{self.child}/reports/", name="/parent/children/<id>/reports/")
