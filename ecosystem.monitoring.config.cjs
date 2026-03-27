module.exports = {
  apps: [
    {
      name: "labbit-monitoring-public",
      cwd: "/opt/labbit-py",
      script: "/opt/labbit-py/scripts/start-monitoring.sh",
      interpreter: "/usr/bin/bash",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        MONITORING_SERVICES_INI: "/opt/labbit-py/services.public.ini",
        MONITORING_LOG_PATH: "/opt/labbit-py/logs/monitoring-public.log",
        CTO_INGEST_TOKEN: ""
      }
    },
    {
      name: "labbit-monitoring-internal",
      cwd: "/opt/labbit-py",
      script: "/opt/labbit-py/scripts/start-monitoring.sh",
      interpreter: "/usr/bin/bash",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        MONITORING_SERVICES_INI: "/opt/labbit-py/services.internal.ini",
        MONITORING_LOG_PATH: "/opt/labbit-py/logs/monitoring-internal.log",
        CTO_INGEST_TOKEN: ""
      }
    }
  ]
};
