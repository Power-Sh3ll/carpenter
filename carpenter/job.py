from carpenter.blueprint import Blueprint


class Job:
    def __init__(self, name, blueprint=None) -> None:
        """
        Create a job with the given name. If blueprint is omitted, the job runs
        whatever default_blueprint the registry it's started in provides;
        passing one here overrides that default for this job only. The job is
        not registered with the registry until register_job() is called, and
        its process is not spawned until start_job() is called.
        """
        # ID is set by the registry when the job is registered
        self.id = None
        self.name = name
        self.blueprint = blueprint
        self.status = "initialized"
        self.process = None
        self.start_time = None

        # validate that the name is a non-empty string
        if not isinstance(name, str) or not name:
            raise ValueError("name must be a non-empty string")
        if len(name) > 255:
            raise ValueError("name must be less than 256 characters")
        if not name.isidentifier():
            raise ValueError("name must be a valid identifier (alphanumeric and underscores only, cannot start with a number)")
        if not name.islower():
            raise ValueError("name must be lowercase")
        if blueprint is not None and not isinstance(blueprint, Blueprint):
            raise ValueError("blueprint must be a Blueprint instance or None")
