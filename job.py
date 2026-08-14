class job:
    def __init__(self, name) -> None:
        """
        Create a job with the given name. The job is not registered with the registry until the registry's start method is called.
        """
        # ID is set by the registry when the job is registered
        self.id = None
        self.name = name
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
        