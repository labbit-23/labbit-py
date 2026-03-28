module.exports = {
  apps: [
    {
      name: "labbit-monitoring-vps",
      cwd: "/opt/labbit-py",
      script: "/opt/labbit-py/scripts/start-monitoring.sh",
      interpreter: "/usr/bin/bash",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        MONITORING_SERVICES_INI: "/opt/labbit-py/services.vps.ini",
        MONITORING_LOG_PATH: "/opt/labbit-py/logs/monitoring-vps.log",
        MONITORING_NODE_ROLE: "vps",
        CTO_INGEST_TOKEN: ""
      }
    },
    {
      name: "labbit-monitoring-local",
      cwd: "/opt/labbit-py",
      script: "/opt/labbit-py/scripts/start-monitoring.sh",
      interpreter: "/usr/bin/bash",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        MONITORING_SERVICES_INI: "/opt/labbit-py/services.local.ini",
        MONITORING_LOG_PATH: "/opt/labbit-py/logs/monitoring-local.log",
        MONITORING_NODE_ROLE: "local",
        CTO_INGEST_TOKEN: ""
      }
    }
  ]
};
