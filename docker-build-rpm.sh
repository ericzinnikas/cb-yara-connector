#!/bin/bash
# docker rmi yaraconnectorrpmbuild --force
docker rm yaraconnectorrpmbuild
docker build --build-arg REBUILD_STEP=`date +%s` -t yaraconnectorrpmbuild .
docker run  -d --name yaraconnectorrpmbuild -it yaraconnectorrpmbuild tail -f /dev/null
docker cp yaraconnectorrpmbuild:/home/cb/rpmbuild/RPMS .
docker stop yaraconnectorrpmbuild 
docker rm yaraconnectorrpmbuild
# docker rmi yaraconnectorrpmbuild --force
