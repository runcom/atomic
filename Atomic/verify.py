from . import util
from . import Atomic
import os
from docker.errors import NotFound
from operator import itemgetter

class Verify(Atomic):
    DEBUG = True

    def verify(self):
        """
        Primary def for atomic verify
        :return: None
        """
        def fix_layers(layers):
            """
            Takes the input of layers (get_layers()) and adds a key
            and value for index.  Also checks if the Tag value is not
            blank but name is, puts tag into name.
            :param layers:
            :return: updated list of layers
            """
            for layer in layers:
                layer['index'] = layers.index(layer)
                if layer['Tag'] is not "" and layer['Name'] is "":
                    layer['Name'] = layer['Tag']
            return layers

        layers = fix_layers(self.get_layers())
        if self.DEBUG:
            for l in layers:
                util.output_json(l)
        uniq_names = list(set(x['Name'] for x in layers if x['Name'] != ''))
        base_images = self.get_tagged_images(uniq_names, layers)
        if self.DEBUG:
            for b in base_images:
                util.output_json(b)
        if self.args.verbose:
            self._print_verify_verbose(base_images, self.image)
        # Do we have any layers that are not up to date?
        elif not all([False for x in base_images if x['local_nvr'] != x['latest_nvr']]):
            self._print_verify(base_images, self.image)
        else:
            # Check if any of the base_images do not have versioning information
            versions = [x['local_nvr'] for x in base_images] + [x['latest_nvr'] for x in base_images]
            if 'Version unavailable' in versions:
                util.writeOut("\nWARNING: One or more of the image layers does not have")
                util.writeOut("{}versioning information. Printing each image layer".format(" " * 9))
                util.writeOut("{}verbosely.".format(" " * 9))
                self._print_verify_verbose(base_images, self.image)
            else:
                # Didn't detect any version differences, do nothing
                pass

    def get_tagged_images(self, names, layers):
        """
        Returns a dict with image names and its tag name.
        :param names:
        :param layers:
        :return: list of sorted dicts (by index)
        """
        base_images = []
        for name in names:
            remote = False
            _match = next((x for x in layers if x['Name'] == name and x['Tag'] is not ''), None)
            local_nvr = ""
            if _match is not None:
                if self.is_repo_from_local_registry(_match['Id']):
                    local_nvr = latest_version = _match['Version']
                    remote = True
                else:
                    local_nvr = self.get_local_latest_version(name)
                    util.writeOut(_match['Tag'])
                    latest_version = self.get_remote_latest_version(_match['Tag'])

                no_version = (latest_version == "")

                iid = _match["Id"]
                tag = _match['Tag']
                _index = self.get_index(name, layers, iid)
            else:
                _index = self.get_index(name, layers)
                layer = layers[_index]
                if layer["Version"] is not "" and layer['Name'] is not "":
                    iid = layer['Id']
                    local_nvr = layer['Version']
                    no_version = False
                    image = self.d.inspect_image(iid)
                    labels = image.get('Config', []).get('Labels', [])
                    if 'Authoritative_Registry' in labels and 'Name' in labels:
                        tag = os.path.join(labels['Authoritative_Registry'], labels['Name'])
                        if self.is_repo_from_local_registry(iid):
                            # Inspect again by tag in case the image isnt the latest
                            try:
                                latest_version = self.d.inspect_image(tag)['Version']
                            except NotFound:
                                latest_version = layer['Version']
                        else:
                            # Do a remote inspect of images
                            latest_version = self.get_remote_latest_version(tag)
                        remote = True
                    else:
                        tag = "Unknown"
                        try:
                            latest_version = self.get_remote_latest_version(name)
                        except NotFound:
                            latest_version = "Unknown"
                else:
                    iid = "Unknown"
                    latest_version = self.get_local_latest_version(name)
                    local_nvr = name
                    tag = "Unknown"
                    remote = False
                    no_version = True
            base_images.append({'iid': iid,
                                'name': name,
                                'local_nvr': local_nvr,
                                'latest_nvr': latest_version,
                                'remote': remote,
                                'no_version': no_version,
                                'tag': tag,
                                'index': _index
                                })
        return sorted(base_images, key=itemgetter('index'))

    def is_repo_from_local_registry(self, input_repo):
        """
        Determine if a given repo comes from a local-only registry
        :param input_repo: str repository name
        :return: bool
        """
        # We need to check if there are other images with the
        # the same IID because the input might not be fully
        # qualified
        iid = self.d.inspect_image(input_repo)['Id']
        # Get a flat list of repo names associated with the iid
        similar = [_repo for repos in [x['RepoTags'] for x in self.d.images()
                                       if x['Id'] == iid] for _repo in repos]
        results = []
        for repo_ in similar:
            (reg, repo, tag) = util._decompose(repo_)
            results.append(self.is_registry_local(reg))
        # if self.DEBUG
            # util.output_json(results)
        return False if not all(results) else True

    def is_registry_local(self, registry):
        """
        Determine if a given registry is local only
        :param registry: str registry name
        :return: bool
        """
        return False if registry in self.get_registries() else True

    def get_registries(self):
        """
        Gets the names of the registries per /etc/sysconfig/conf
        :return: a list of the registries
        """
        registries = []
        docker_info = self.d.info()
        if 'RegistryConfig' not in docker_info:
            raise ValueError("This docker version does not export its registries.")
        for _index in docker_info['RegistryConfig']['IndexConfigs']:
            registries.append(_index)
        return registries

    @staticmethod
    def _print_verify(base_images, image):
        """
        The standard non-verbose print for atomic verify
        :param base_images:
        :param image:
        :return:  None
        """
        util.writeOut("\n{} contains images or layers that have updates:".format(image))
        for _image in base_images:
            local = _image['local_nvr']
            latest = _image['latest_nvr']
            if local != latest:
                util.writeOut("\n{0} '{1}' has an update to '{2}'"
                              .format(" " * 5, local, latest))
        util.writeOut("\n")

    @staticmethod
    def _print_verify_verbose(base_images, image):
        """
        Implements a verbose printout of layers.  Can be called with
        atomic verify -v or if we detect some layer does not have
        versioning information.
        :param base_images:
        :param image:
        :return: None
        """
        def max_name(base_images):
            no_version_match = [len(x['tag']) + len(x['local_nvr']) + 5 for x in base_images if x['no_version']]
            return max([len(x['local_nvr']) for x in base_images] + no_version_match)
        _max = max_name(base_images)
        _max_name = 30 if _max < 30 else _max
        three_col = "     {0:" + \
                    str(_max_name) + "} {1:" + \
                    str(_max_name) + "} {2:1}"
        util.writeOut("\n{} contains the following images:\n".format(image))
        util.writeOut(three_col.format("Local Version", "Latest Version", ""))
        util.writeOut(three_col.format("-------------", "--------------", ""))
        for _image in base_images:
            local = _image['local_nvr']
            latest = _image['latest_nvr']
            if _image['no_version']:
                tag = _image['tag']
                local = "{0} ({1})".format(tag, local)
                latest = "{0} ({1})".format(tag,  latest)
            remote = "*" if local != latest else ""
            util.writeOut(three_col.format(local, latest, remote))
        util.writeOut("\n     * = version difference\n")

    @staticmethod
    def get_index(name, layers, _id="0"):
        """
        Adds indexs to the base_image dict and returns sorted
        :param name:
        :param layers:
        :param _id:
        :return: sorted list of base_images
        """
        try:
            try:
                _match = (x for x in layers if x["Id"] == _id).__next__()
            except:
                _match = (x for x in layers if x["Id"] == _id).next()
        except StopIteration:
            # We were unable to associate IDs due to the local image being set
            # to intermediate by docker bc it is outdated. Therefore we find
            # the first instance by name for the index
            try:
                _match = (x for x in layers if x["Name"] == name).__next__()
            except:
                _match = (x for x in layers if x["Name"] == name).next()
        return _match['index']

    def get_local_latest_version(self, name):
        """
        Obtain the latest version of a local image
        :param name:
        :return: str of vnr
        """
        images = self.get_images()
        for image in images:
            if 'Labels' in image and image['Labels'] is not None:
                if self.pull_label(image, 'Name') == name:
                    return self.assemble_nvr(image)
            else:
                continue
        return "Version unavailable"

    def get_remote_latest_version(self, tag):
        r_inspect = self.d.remote_inspect(tag)
        if 'Labels' in r_inspect['Config'] \
                and r_inspect['Config']['Labels'] is not None:
            latest_version = self.assemble_nvr(r_inspect['Config'])
        else:
            latest_version = "Version unavailable"
        return latest_version

    def assemble_nvr(self, image):
        """
        Simple formatting def for NVR
        :param image:
        :return: str
        """

        name = self.pull_label(image, 'Name')
        version = self.pull_label(image, 'Version')
        release = self.pull_label(image, 'Release')
        nvr = "%s-%s-%s" % (name, version, release)

        if any(True for x in [name, version, release] if x is None):
            return "Version unavailable"
        else:
            return nvr

    @staticmethod
    def get_local_version(name, layers):
        for layer in layers:
            if layer['Name'] is name:
                return layer['Version'] if 'Version' in layer \
                    else "Version unavailable"

    @staticmethod
    def pull_label(image, key):
        if key in image["Labels"]:
            return image['Labels'][key]
