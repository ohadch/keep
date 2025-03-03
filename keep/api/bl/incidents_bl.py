import logging
import os
import pathlib
import sys
from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException
from pusher import Pusher
from sqlmodel import Session

from keep.api.arq_pool import get_pool
from keep.api.core.db import (
    add_alerts_to_incident_by_incident_id,
    create_incident_from_dto,
    delete_incident_by_id,
    get_incident_alerts_by_incident_id,
    get_incident_by_id,
    get_incident_unique_fingerprint_count,
    remove_alerts_to_incident_by_incident_id,
    update_incident_from_dto_by_id,
)
from keep.api.core.elastic import ElasticClient
from keep.api.models.alert import IncidentDto, IncidentDtoIn
from keep.api.models.db.alert import Incident
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
from keep.workflowmanager.workflowmanager import WorkflowManager

MIN_INCIDENT_ALERTS_FOR_SUMMARY_GENERATION = int(
    os.environ.get("MIN_INCIDENT_ALERTS_FOR_SUMMARY_GENERATION", 5)
)

ee_enabled = os.environ.get("EE_ENABLED", "false") == "true"
if ee_enabled:
    path_with_ee = (
        str(pathlib.Path(__file__).parent.resolve()) + "/../../../ee/experimental"
    )
    sys.path.insert(0, path_with_ee)
else:
    ALGORITHM_VERBOSE_NAME = NotImplemented


class IncidentBl:
    def __init__(
        self, tenant_id: str, session: Session, pusher_client: Optional[Pusher] = None
    ):
        self.tenant_id = tenant_id
        self.session = session
        self.pusher_client = pusher_client
        self.logger = logging.getLogger(__name__)
        self.ee_enabled = os.environ.get("EE_ENABLED", "false").lower() == "true"
        self.redis = os.environ.get("REDIS", "false") == "true"

    def create_incident(
        self, incident_dto: IncidentDtoIn, generated_from_ai: bool = False
    ) -> IncidentDto:
        """
        Creates a new incident.

        Args:
            incident_dto (IncidentDtoIn): The data transfer object containing the details of the incident to be created.
            generated_from_ai (bool, optional): Indicates if the incident was generated by Keep's AI. Defaults to False.

        Returns:
            IncidentDto: The newly created incident object, containing details of the incident.
        """
        self.logger.info(
            "Creating incident",
            extra={"incident_dto": incident_dto.dict(), "tenant_id": self.tenant_id},
        )
        incident = create_incident_from_dto(
            self.tenant_id, incident_dto, generated_from_ai=generated_from_ai
        )
        self.logger.info(
            "Incident created",
            extra={"incident_id": incident.id, "tenant_id": self.tenant_id},
        )
        new_incident_dto = IncidentDto.from_db_incident(incident)
        self.logger.info(
            "Incident DTO created",
            extra={"incident_id": new_incident_dto.id, "tenant_id": self.tenant_id},
        )
        self.__update_client_on_incident_change()
        self.logger.info(
            "Client updated on incident change",
            extra={"incident_id": new_incident_dto.id, "tenant_id": self.tenant_id},
        )
        self.__run_workflows(new_incident_dto, "created")
        self.logger.info(
            "Workflows run on incident",
            extra={"incident_id": new_incident_dto.id, "tenant_id": self.tenant_id},
        )
        return new_incident_dto

    async def add_alerts_to_incident(
        self, incident_id: UUID, alert_ids: List[UUID], is_created_by_ai: bool = False
    ) -> None:
        self.logger.info(
            "Adding alerts to incident",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )
        incident = get_incident_by_id(tenant_id=self.tenant_id, incident_id=incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")

        add_alerts_to_incident_by_incident_id(self.tenant_id, incident_id, alert_ids, is_created_by_ai)
        self.logger.info(
            "Alerts added to incident",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )
        self.__update_elastic(incident_id, alert_ids)
        self.logger.info(
            "Alerts pushed to elastic",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )
        self.__update_client_on_incident_change(incident_id)
        self.logger.info(
            "Client updated on incident change",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )
        incident_dto = IncidentDto.from_db_incident(incident)
        self.__run_workflows(incident_dto, "updated")
        self.logger.info(
            "Workflows run on incident",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )
        await self.__generate_summary(incident_id, incident)
        self.logger.info(
            "Summary generated",
            extra={"incident_id": incident_id, "alert_ids": alert_ids},
        )

    def __update_elastic(self, incident_id: UUID, alert_ids: List[UUID]):
        try:
            elastic_client = ElasticClient(self.tenant_id)
            if elastic_client.enabled:
                db_alerts, _ = get_incident_alerts_by_incident_id(
                    tenant_id=self.tenant_id,
                    incident_id=incident_id,
                    limit=len(alert_ids),
                )
                enriched_alerts_dto = convert_db_alerts_to_dto_alerts(
                    db_alerts, with_incidents=True
                )
                elastic_client.index_alerts(alerts=enriched_alerts_dto)
        except Exception:
            self.logger.exception("Failed to push alert to elasticsearch")

    def __update_client_on_incident_change(self, incident_id: Optional[UUID] = None):
        if self.pusher_client is not None:
            self.logger.info(
                "Pushing incident change to client",
                extra={"incident_id": incident_id, "tenant_id": self.tenant_id},
            )
            self.pusher_client.trigger(
                f"private-{self.tenant_id}",
                "incident-change",
                {"incident_id": str(incident_id) if incident_id else None},
            )
            self.logger.info(
                "Incident change pushed to client",
                extra={"incident_id": incident_id, "tenant_id": self.tenant_id},
            )

    def __run_workflows(self, incident_dto: IncidentDto, action: str):
        try:
            workflow_manager = WorkflowManager.get_instance()
            workflow_manager.insert_incident(self.tenant_id, incident_dto, action)
        except Exception:
            self.logger.exception(
                "Failed to run workflows based on incident",
                extra={"incident_id": incident_dto.id, "tenant_id": self.tenant_id},
            )

    async def __generate_summary(self, incident_id: UUID, incident: Incident):
        try:
            fingerprints_count = get_incident_unique_fingerprint_count(
                self.tenant_id, incident_id
            )
            if (
                ee_enabled
                and self.redis
                and fingerprints_count > MIN_INCIDENT_ALERTS_FOR_SUMMARY_GENERATION
                and not incident.user_summary
            ):
                pool = await get_pool()
                job = await pool.enqueue_job(
                    "process_summary_generation",
                    tenant_id=self.tenant_id,
                    incident_id=incident_id,
                )
                self.logger.info(
                    f"Summary generation for incident {incident_id} scheduled, job: {job}",
                    extra={
                        "tenant_id": self.tenant_id,
                        "incident_id": incident_id,
                    },
                )
        except Exception:
            self.logger.exception(
                "Failed to generate summary for incident",
                extra={"incident_id": incident_id, "tenant_id": self.tenant_id},
            )

    def delete_alerts_from_incident(
        self, incident_id: UUID, alert_ids: List[UUID]
    ) -> None:
        self.logger.info(
            "Fetching incident",
            extra={
                "incident_id": incident_id,
                "tenant_id": self.tenant_id,
            },
        )
        incident = get_incident_by_id(tenant_id=self.tenant_id, incident_id=incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")

        remove_alerts_to_incident_by_incident_id(self.tenant_id, incident_id, alert_ids)

    def delete_incident(self, incident_id: UUID) -> None:
        self.logger.info(
            "Fetching incident",
            extra={
                "incident_id": incident_id,
                "tenant_id": self.tenant_id,
            },
        )

        incident = get_incident_by_id(tenant_id=self.tenant_id, incident_id=incident_id)
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")

        incident_dto = IncidentDto.from_db_incident(incident)

        deleted = delete_incident_by_id(
            tenant_id=self.tenant_id, incident_id=incident_id
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Incident not found")
        self.__update_client_on_incident_change()
        try:
            workflow_manager = WorkflowManager.get_instance()
            self.logger.info("Adding incident to the workflow manager queue")
            workflow_manager.insert_incident(self.tenant_id, incident_dto, "deleted")
            self.logger.info("Added incident to the workflow manager queue")
        except Exception:
            self.logger.exception(
                "Failed to run workflows based on incident",
                extra={"incident_id": incident_dto.id, "tenant_id": self.tenant_id},
            )

    def update_incident(
        self,
        incident_id: UUID,
        updated_incident_dto: IncidentDtoIn,
        generated_by_ai: bool,
    ) -> None:
        self.logger.info(
            "Fetching incident",
            extra={
                "incident_id": incident_id,
                "tenant_id": self.tenant_id,
            },
        )
        incident = update_incident_from_dto_by_id(
            self.tenant_id, incident_id, updated_incident_dto, generated_by_ai
        )
        if not incident:
            raise HTTPException(status_code=404, detail="Incident not found")

        new_incident_dto = IncidentDto.from_db_incident(incident)
        try:
            workflow_manager = WorkflowManager.get_instance()
            self.logger.info("Adding incident to the workflow manager queue")
            workflow_manager.insert_incident(
                self.tenant_id, new_incident_dto, "updated"
            )
            self.logger.info("Added incident to the workflow manager queue")
        except Exception:
            self.logger.exception(
                "Failed to run workflows based on incident",
                extra={"incident_id": new_incident_dto.id, "tenant_id": self.tenant_id},
            )
        return new_incident_dto
