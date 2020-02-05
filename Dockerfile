FROM ubuntu:18.04 as builder
ENV TERM linux
ENV DEBIAN_FRONTEND noninteractive
COPY . /opt/mycroft/mycroft-core
# Install Server Dependencies for Mycroft
RUN set -x \
    # Un-comment any package sources that include a multiverse
	&& sed -i 's/# \(.*multiverse$\)/\1/g' /etc/apt/sources.list \
	&& apt-get update \
	# Install packages specific to Docker implementation
    && apt-get -y install locales sudo\
	&& mkdir /opt/mycroft/skills \
	&& CI=true bash -x /opt/mycroft/mycroft-core/dev_setup.sh --allow-root -sm \
	&& apt-get -y autoremove \
	&& apt-get clean \
	&& rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
# Set the locale
RUN locale-gen en_US.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8
# Add the local configuration directory
RUN mkdir ~/.mycroft
EXPOSE 8181

# Integration Test Suite
FROM builder as voigt_kampff
# Add the mycroft core virtual environment to the system path.
ENV PATH /opt/mycroft/mycroft-core/.venv/bin:$PATH
WORKDIR /opt/mycroft/mycroft-core/test/integrationtests/voigt_kampff
# Install Mark I default skills
RUN msm -p mycroft_mark_1 default
# The behave feature files for a skill are defined within the skill's
# repository.  Copy those files into the local feature file directory
# for test discovery.
RUN python -m test.integrationtests.voigt_kampff.skill_setup -c default.yml
# Setup and run the integration tests
ENTRYPOINT "./run_test_suite.sh"
