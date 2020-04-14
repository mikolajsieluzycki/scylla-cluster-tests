from datetime import datetime
from logging import getLogger
from sdcm.send_email import Email
from sdcm.utils.common import list_instances_aws, list_instances_gce, aws_tags_to_dict, gce_meta_to_dict
from sdcm.utils.cloud_monitor.report import GeneralReport, DetailedReport, NA


LOGGER = getLogger(__name__)


class CloudInstance:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    def __init__(self, cloud, name, region_az, state, lifecycle, instance_type, owner, create_time, keep):  # pylint: disable=too-many-arguments
        self.cloud = cloud
        self.name = name
        self.region_az = region_az
        self.state = state
        self.lifecycle = lifecycle
        self.instance_type = instance_type
        self.owner = owner.lower()
        self.create_time = create_time
        self.keep = keep  # keep alive


class CloudInstances:
    CLOUD_PROVIDERS = ("aws", "gce")

    def __init__(self):
        self.instances = {prov: [] for prov in self.CLOUD_PROVIDERS}
        self.all_instances = []
        self.get_all()

    def __getitem__(self, item):
        return self.instances[item]

    @staticmethod
    def get_keep_alive_gce_instance(instance):
        # checking labels
        labels = instance.extra["labels"]
        if labels:
            return labels.get("keep", labels.get("keep-alive", ""))
        # checking tags
        tags = instance.extra["tags"]
        if tags:
            return "alive" if 'alive' in tags or 'keep-alive' in tags or 'keep' in tags else ""
        return ""

    def get_aws_instances(self):
        aws_instances = list_instances_aws(verbose=True)
        for instance in aws_instances:
            tags = aws_tags_to_dict(instance.get('Tags'))
            cloud_instance = CloudInstance(
                cloud="aws",
                name=tags.get("Name", NA),
                region_az=instance["Placement"]["AvailabilityZone"],
                state=instance["State"]["Name"],
                lifecycle="spot" if instance.get("SpotInstanceRequestId") else "on-demand",
                instance_type=instance["InstanceType"],
                owner=tags.get("RunByUser", tags.get("Owner", NA)),
                create_time=instance['LaunchTime'].ctime(),
                keep=tags.get("keep", ""),
            )
            self.instances["aws"].append(cloud_instance)
        self.all_instances += self.instances["aws"]

    def get_gce_instances(self):
        gce_instances = list_instances_gce(verbose=True)
        for instance in gce_instances:
            tags = gce_meta_to_dict(instance.extra['metadata'])
            cloud_instance = CloudInstance(
                cloud="gce",
                name=instance.name,
                region_az=instance.extra["zone"].name,
                state=instance.state,
                lifecycle="spot" if instance.extra["scheduling"]["preemptible"] else "on-demand",
                instance_type=instance.size,
                owner=tags.get("RunByUser", NA) if tags else NA,
                create_time=instance.extra['creationTimestamp'],
                keep=self.get_keep_alive_gce_instance(instance)
            )
            self.instances["gce"].append(cloud_instance)
        self.all_instances += self.instances["gce"]

    def get_all(self):
        LOGGER.info("Getting all cloud instances...")
        self.get_aws_instances()
        self.get_gce_instances()


def notify_by_email(general_report: GeneralReport, detailed_report: DetailedReport, recipients: list):
    email_client = Email()
    LOGGER.info("Sending email to '%s'", recipients)
    email_client.send(subject="Cloud resources: usage report - {}".format(datetime.now()),
                      content=general_report.to_html(),
                      recipients=recipients,
                      html=True,
                      files=[detailed_report.to_file()]
                      )


def cloud_report(mail_to):
    cloud_instances = CloudInstances()
    notify_by_email(general_report=GeneralReport(cloud_instances),
                    detailed_report=DetailedReport(cloud_instances),
                    recipients=mail_to)
