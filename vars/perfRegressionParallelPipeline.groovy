#!groovy
import groovy.json.JsonSlurper

def (testDuration, testRunTimeout, runnerTimeout, collectLogsTimeout, resourceCleanupTimeout) = [0,0,0,0,0]

def call(Map pipelineParams) {
    def builder = getJenkinsLabels(params.backend, params.region, params.gce_datacenter)

    pipeline {
        agent none

        environment {
            AWS_ACCESS_KEY_ID     = credentials('qa-aws-secret-key-id')
            AWS_SECRET_ACCESS_KEY = credentials('qa-aws-secret-access-key')
		}
        parameters {
            string(defaultValue: "${pipelineParams.get('backend', 'aws')}",
               description: 'aws|gce',
               name: 'backend')

            string(defaultValue: "${pipelineParams.get('region', 'eu-west-1')}",
               description: 'us-east-1|eu-west-1',
               name: 'region')

            string(defaultValue: "${pipelineParams.get('availability_zone', 'a')}",
                description: 'Availability zone',
                name: 'availability_zone')

            string(defaultValue: "${pipelineParams.get('gce_datacenter', 'us-east1')}",
                   description: 'GCE datacenter',
                   name: 'gce_datacenter')

            string(defaultValue: '', description: '', name: 'scylla_ami_id')
            string(defaultValue: '', description: '', name: 'scylla_version')
            string(defaultValue: '', description: '', name: 'scylla_repo')
            string(defaultValue: "${pipelineParams.get('provision_type', 'spot')}",
                   description: 'spot_low_price|on_demand|spot_fleet|spot_low_price|spot_duration|spot',
                   name: 'provision_type')

            string(defaultValue: "${pipelineParams.get('post_behavior_db_nodes', 'destroy')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_db_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_loader_nodes', 'destroy')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_loader_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_monitor_nodes', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_monitor_nodes')
            string(defaultValue: "${pipelineParams.get('post_behavior_k8s_cluster', 'keep-on-failure')}",
                   description: 'keep|keep-on-failure|destroy',
                   name: 'post_behavior_k8s_cluster')
            string(defaultValue: "${groovy.json.JsonOutput.toJson(pipelineParams.get('sub_tests'))}",
                   description: 'subtests in format ["sub_test1", "sub_test2"] or empty',
                   name: 'sub_tests')

            string(defaultValue: "${pipelineParams.get('email_recipients', 'scylla-perf-results@scylladb.com')}",
                   description: 'email recipients of email report',
                   name: 'email_recipients')

            string(defaultValue: "${pipelineParams.get('test_config', '')}",
                   description: 'Test configuration file',
                   name: 'test_config')

            string(defaultValue: "${pipelineParams.get('k8s_scylla_operator_helm_repo', 'https://storage.googleapis.com/scylla-operator-charts/latest')}",
                   description: 'Scylla Operator helm repo',
                   name: 'k8s_scylla_operator_helm_repo')
            string(defaultValue: "${pipelineParams.get('k8s_scylla_operator_chart_version', 'latest')}",
                   description: 'Scylla Operator helm chart version',
                   name: 'k8s_scylla_operator_chart_version')
            string(defaultValue: "${pipelineParams.get('k8s_scylla_operator_docker_image', '')}",
                   description: 'Scylla Operator docker image',
                   name: 'k8s_scylla_operator_docker_image')
        }
        options {
            timestamps()
            disableConcurrentBuilds()
            buildDiscarder(logRotator(numToKeepStr: '20'))
        }
        stages {
            stage("Preparation") {
                // NOTE: this stage is a workaround for the following Jenkins bug:
                // https://issues.jenkins-ci.org/browse/JENKINS-41929
                when { expression { env.BUILD_NUMBER == '1' } }
                steps {
                    script {
                        if (currentBuild.getBuildCauses('hudson.model.Cause$UserIdCause') != null) {
                            currentBuild.description = ('Aborted build#1 not having parameters loaded. \n'
                              + 'Build#2 is ready to run')
                            currentBuild.result = 'ABORTED'

                            error('Abort build#1 which only loads params')
                        }
                    }
                }
            }
            stage('Get test duration') {
                agent {
                    label {
                        label builder.label
                    }
                }
                steps {
                    catchError(stageResult: 'FAILURE') {
                        timeout(time: 5, unit: 'MINUTES') {
                            script {
                                wrap([$class: 'BuildUser']) {
                                    dir('scylla-cluster-tests') {
                                        checkout scm
                                        (testDuration, testRunTimeout, runnerTimeout, collectLogsTimeout, resourceCleanupTimeout) = getJobTimeouts(params, builder.region)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            stage('Run SCT Performance Tests') {
                steps {
                    script {
                        def tasks = [:]
                        def sub_tests
                        if (params.sub_tests) {
                            sub_tests = new JsonSlurper().parseText(params.sub_tests)
                        } else {
                            sub_tests = [pipelineParams.test_name]
                        }
                        for (t in sub_tests) {
                            def perf_test
                            def sub_test = t
                            if (sub_test == pipelineParams.test_name) {
                                perf_test = sub_test
                            } else {
                                perf_test = "${pipelineParams.test_name}.${sub_test}"
                            }

                            tasks["sub_test=${sub_test}"] = {
                                node(builder.label) {
                                    withEnv(["AWS_ACCESS_KEY_ID=${env.AWS_ACCESS_KEY_ID}",
                                             "AWS_SECRET_ACCESS_KEY=${env.AWS_SECRET_ACCESS_KEY}",
                                             "SCT_TEST_ID=${UUID.randomUUID().toString()}",]) {
                                        stage("Checkout for ${sub_test}") {
                                            catchError(stageResult: 'FAILURE') {
                                                timeout(time: 5, unit: 'MINUTES') {
                                                    script {
                                                        wrap([$class: 'BuildUser']) {
                                                            dir('scylla-cluster-tests') {
                                                                checkout scm
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        stage("Create SCT Runner for ${sub_test}") {
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    dir('scylla-cluster-tests') {
                                                        timeout(time: 5, unit: 'MINUTES') {
                                                            createSctRunner(params, runnerTimeout, builder.region)
                                                        }
                                                    }
                                                }
                                            }
                                        }

                                        stage("Run ${sub_test}"){
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    def email_recipients = groovy.json.JsonOutput.toJson(params.email_recipients)
                                                    def test_config = groovy.json.JsonOutput.toJson(pipelineParams.test_config)
                                                    timeout(time: testRunTimeout, unit: 'MINUTES') { dir('scylla-cluster-tests') {

                                                        sh """
                                                        #!/bin/bash
                                                        set -xe
                                                        env

                                                        rm -fv ./latest

                                                        export SCT_CLUSTER_BACKEND=${params.backend}
                                                        if [[ -n "${params.region}" ]]; then
                                                            export SCT_REGION_NAME=${params.region}
                                                        else
                                                            export SCT_REGION_NAME=${pipelineParams.region}
                                                        fi
                                                        export SCT_CONFIG_FILES=${test_config}

                                                        export SCT_AVAILABILITY_ZONE="${params.availability_zone}"

                                                        if [[ -n "${params.gce_datacenter ? params.gce_datacenter : ''}" ]] ; then
                                                            export SCT_GCE_DATACENTER=${params.gce_datacenter}
                                                        fi

                                                        export SCT_EMAIL_RECIPIENTS="${email_recipients}"
                                                        if [[ ! -z "${params.scylla_ami_id}" ]] ; then
                                                            export SCT_AMI_ID_DB_SCYLLA=${params.scylla_ami_id}
                                                        elif [[ ! -z "${params.scylla_version}" ]] ; then
                                                            export SCT_SCYLLA_VERSION=${params.scylla_version}
                                                        elif [[ ! -z "${params.scylla_repo}" ]] ; then
                                                            export SCT_SCYLLA_REPO=${params.scylla_repo}
                                                        elif [[ "${params.backend ? params.backend : ''}" == *"k8s"* ]] ; then
                                                            echo "Kubernetes backend can have empty scylla version. It will be taken from defaults of the scylla helm chart"
                                                        else
                                                            echo "need to choose one of SCT_AMI_ID_DB_SCYLLA | SCT_SCYLLA_VERSION | SCT_SCYLLA_REPO"
                                                            exit 1
                                                        fi


                                                        export SCT_POST_BEHAVIOR_DB_NODES="${params.post_behavior_db_nodes}"
                                                        export SCT_POST_BEHAVIOR_LOADER_NODES="${params.post_behavior_loader_nodes}"
                                                        export SCT_POST_BEHAVIOR_MONITOR_NODES="${params.post_behavior_monitor_nodes}"
                                                        export SCT_POST_BEHAVIOR_K8S_CLUSTER="${params.post_behavior_k8s_cluster}"
                                                        export SCT_INSTANCE_PROVISION=${params.provision_type}
                                                        export SCT_AMI_ID_DB_SCYLLA_DESC=\$(echo \$GIT_BRANCH | sed -E 's+(origin/|origin/branch-)++')
                                                        export SCT_AMI_ID_DB_SCYLLA_DESC=\$(echo \$SCT_AMI_ID_DB_SCYLLA_DESC | tr ._ - | cut -c1-8 )

                                                        if [[ -n "${params.k8s_scylla_operator_helm_repo ? params.k8s_scylla_operator_helm_repo : ''}" ]] ; then
                                                            export SCT_K8S_SCYLLA_OPERATOR_HELM_REPO=${params.k8s_scylla_operator_helm_repo}
                                                        fi
                                                        if [[ -n "${params.k8s_scylla_operator_chart_version ? params.k8s_scylla_operator_chart_version : ''}" ]] ; then
                                                            export SCT_K8S_SCYLLA_OPERATOR_CHART_VERSION=${params.k8s_scylla_operator_chart_version}
                                                        fi
                                                        if [[ -n "${params.k8s_scylla_operator_docker_image ? params.k8s_scylla_operator_docker_image : ''}" ]] ; then
                                                            export SCT_K8S_SCYLLA_OPERATOR_DOCKER_IMAGE=${params.k8s_scylla_operator_docker_image}
                                                        fi
                                                        if [[ -n "${pipelineParams.k8s_deploy_monitoring ? pipelineParams.k8s_deploy_monitoring : ''}" ]] ; then
                                                            export SCT_K8S_DEPLOY_MONITORING=${pipelineParams.k8s_deploy_monitoring}
                                                        fi
                                                        if [[ -n "${pipelineParams.k8s_enable_performance_tuning ? pipelineParams.k8s_enable_performance_tuning : ''}" ]] ; then
                                                            export SCT_K8S_ENABLE_PERFORMANCE_TUNING=${pipelineParams.k8s_enable_performance_tuning}
                                                        fi
                                                        if [[ -n "${pipelineParams.k8s_scylla_utils_docker_image ? pipelineParams.k8s_scylla_utils_docker_image : ''}" ]] ; then
                                                            export SCT_K8S_SCYLLA_UTILS_DOCKER_IMAGE=${pipelineParams.k8s_scylla_utils_docker_image}
                                                        fi

                                                        echo "start test ......."
                                                        SCT_RUNNER_IP=\$(cat sct_runner_ip||echo "")
                                                        if [[ -n "\${SCT_RUNNER_IP}" ]] ; then
                                                            ./docker/env/hydra.sh --execute-on-runner \${SCT_RUNNER_IP} run-test ${perf_test} --backend ${params.backend}
                                                        else
                                                            ./docker/env/hydra.sh run-test ${perf_test} --backend ${params.backend}  --logdir "`pwd`"
                                                        fi
                                                        echo "end test ....."
                                                        """
                                                    }}
                                                }
                                            }
                                        }
                                        stage("Collect logs for ${sub_test}") {
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    timeout(time: collectLogsTimeout, unit: 'MINUTES') {
                                                        dir('scylla-cluster-tests') {
                                                            runCollectLogs(params, builder.region)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        stage("Clean resources for ${sub_test}") {
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    dir('scylla-cluster-tests') {
                                                        timeout(time: resourceCleanupTimeout, unit: 'MINUTES') {
                                                            runCleanupResource(params, builder.region)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        stage("Send email for ${sub_test}") {
                                            def email_recipients = groovy.json.JsonOutput.toJson(params.email_recipients)
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    dir('scylla-cluster-tests') {
                                                        timeout(time: 10, unit: 'MINUTES') {
                                                            runSendEmail(params, currentBuild)
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        stage('Clean SCT Runners') {
                                            catchError(stageResult: 'FAILURE') {
                                                wrap([$class: 'BuildUser']) {
                                                    dir('scylla-cluster-tests') {
                                                        cleanSctRunners(params, currentBuild)
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        parallel tasks
                    }
                }
            }
        }
    }
}
