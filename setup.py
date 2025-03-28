from setuptools import setup, find_packages

setup(
    name='pyephember2',
    version='0.4.9',
    description='Python library to work with ember from EPH Controls',
    keywords='ephember',
    author='Robert Young',
    author_email='youngro@tcd.ie',
    license='MIT',
    url='https://github.com/roberty99/pyephember2',
    download_url='https://github.com/roberty99/pyephember2/archive/0.4.3.tar.gz',
    platforms=["any"],
    packages=find_packages(),
    use_scm_version=True,
    zip_safe=False,
    install_requires=[
        'requests',
        'paho-mqtt'
    ],
    test_requires=[
        'tox',
        'flake8',
        'pylint'
    ]
)
