"""Явний координатор сценарію перенесення події (оркестрація)."""
import logging
import time

from soa_common.http_client import ServiceClient, ServiceUnavailableError

log = logging.getLogger("orchestrator")


class RescheduleOrchestrator:
    """Послідовно викликає кроки інших сервісів і збирає результат для організатора.

    Порядок кроків задано тут, а не розподілено між підписниками: учасники
    не знають один про одного, координатор знає весь сценарій.
    """

    def __init__(self, participation: ServiceClient, notification: ServiceClient, coordinator: str) -> None:
        self._steps = [("reset-invitations", participation), ("move-reminders", notification)]
        self._coordinator = coordinator

    async def run(self, snapshot: dict) -> dict:
        path = f"/internal/events/{snapshot['eventId']}/reschedule"
        results = []
        failed = False
        for step, client in self._steps:
            if failed:
                results.append({"step": step, "service": client.service_name, "status": "SKIPPED"})
                continue
            started = time.perf_counter()
            result = {"step": step, "service": client.service_name}
            try:
                response = await client.request("POST", path, json=snapshot)
                result["instance"] = response.headers.get("X-Served-By")
                result["httpStatus"] = response.status_code
                if response.is_success:
                    result.update(status="DONE", result=response.json())
                else:
                    result.update(status="FAILED", error=response.json())
            except ServiceUnavailableError as exc:
                result.update(status="FAILED", error=str(exc))
            result["durationMs"] = round((time.perf_counter() - started) * 1000, 1)
            failed = result["status"] == "FAILED"
            results.append(result)
            log.info("Крок %s: %s", step, result["status"])

        report = {
            "pattern": "orchestration",
            "coordinator": self._coordinator,
            "status": "FAILED" if failed else "COMPLETED",
            "steps": results,
        }
        if failed:
            report["note"] = "Виконані кроки не відкочено; компенсації SAGA передбачено в ЛР3"
        return report
