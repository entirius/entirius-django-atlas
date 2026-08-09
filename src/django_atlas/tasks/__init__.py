from django_atlas.tasks.feed_dispatch import dispatch_scheduled_feeds_task
from django_atlas.tasks.feed_execution import execute_feed_task
from django_atlas.tasks.image_download import download_source_images_task
from django_atlas.tasks.import_log_retention import prune_import_logs_task
from django_atlas.tasks.observation_retention import prune_observations_task
from django_atlas.tasks.primary_strategy import evaluate_primary_sources_task
from django_atlas.tasks.push_pipeline import push_approved_for_source_task
from django_atlas.tasks.retention import prune_source_events_task
from django_atlas.tasks.scraper_callback import process_scraper_results_task

__all__ = [
    "dispatch_scheduled_feeds_task",
    "download_source_images_task",
    "evaluate_primary_sources_task",
    "execute_feed_task",
    "process_scraper_results_task",
    "prune_import_logs_task",
    "prune_observations_task",
    "prune_source_events_task",
    "push_approved_for_source_task",
]
